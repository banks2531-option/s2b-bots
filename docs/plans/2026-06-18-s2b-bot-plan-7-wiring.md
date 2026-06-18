# S2b Bot — Plan 7: Go-Live Wiring (feeds, deps assembly, runner)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the tested core into a bot that can run against Tradier **sandbox**: production data adapters (parse Tradier chain/balances/positions, compute ATR, VIX regime, pick the weekly expiry), a leg-aware live reconcile, `build_deps` that assembles a production `Deps`, and a thin `runner` that calls `tick()` on a cadence. Adapters are pure/fixture-testable; only the scheduler sleep loop is untested glue.

**Architecture:** `bot/app/feeds.py` = pure parsers + calculators (inject the Tradier JSON / price bars / VIX series). `bot/app/wiring.py` = `build_deps(http, account_id, ...)` assembling a `Deps` from those adapters and the Plan-2 broker client, plus `reconcile_live` (verifies the bot's tracked spreads' legs are present at the broker, flags untracked legs) and a `runner(state, deps, now_fn, sleep_fn, ticks)` loop. Credentials come only from env via `make_http_from_env` (C8).

**Tech Stack:** Python 3.11, pytest, dataclasses/datetime (stdlib). Reuses every prior module + `bot.broker.tradier`, `bot.strategy.s2b._occ`.

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` §3/§5 (entry inputs), §6 (env creds), §7 (reconcile), §8 (sandbox first).

---

### Task 1: feeds package + pick_weekly_expiry

**Files:** Create `bot/app/feeds.py`, `bot/tests/test_feeds.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_feeds.py`:
```python
from bot.app.feeds import pick_weekly_expiry


def test_pick_weekly_expiry_next_friday_min_dte():
    # Monday 2026-06-15: the Friday 2026-06-19 is 4 DTE -> ok
    assert pick_weekly_expiry("2026-06-15", min_dte=4) == "2026-06-19"


def test_pick_weekly_expiry_skips_too_near_friday():
    # Thursday 2026-06-18: that-week Friday (06-19) is only 1 DTE -> roll to next Friday 06-26
    assert pick_weekly_expiry("2026-06-18", min_dte=4) == "2026-06-26"
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.app.feeds`.

- [ ] **Step 3: Implement** — `bot/app/feeds.py`:
```python
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
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_feeds.py -v` → 2 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/app/feeds.py bot/tests/test_feeds.py
git commit -m "feat: pick_weekly_expiry"
```

---

### Task 2: parse_chain → [OptionQuote]

**Files:** Modify `bot/app/feeds.py`, `bot/tests/test_feeds.py`

- [ ] **Step 1: Add failing test** (append):
```python
from bot.app.feeds import parse_chain
from bot.strategy.s2b import OptionQuote

CHAIN_RESP = {"options": {"option": [
    {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
    {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
    {"strike": 580.0, "option_type": "call", "bid": 2.00, "ask": 2.10, "greeks": {"delta": 0.40}},
    {"strike": 560.0, "option_type": "put", "bid": 2.00, "ask": 2.10, "greeks": {"delta": None}},
]}}


def test_parse_chain_puts_only_abs_delta():
    quotes = parse_chain(CHAIN_RESP)
    # calls excluded; the null-delta put excluded; two clean puts remain with ABS delta
    assert quotes == [OptionQuote(568.0, 0.36, 3.40, 3.50), OptionQuote(558.0, 0.18, 1.60, 1.70)]
```

- [ ] **Step 2: Run, expect fail** — `ImportError: parse_chain`.

- [ ] **Step 3: Implement** (append to `bot/app/feeds.py`):
```python
from bot.strategy.s2b import OptionQuote


def parse_chain(resp) -> list:
    """Tradier options-chain JSON -> [OptionQuote] for PUTS with a usable delta (abs)."""
    options = (resp.get("options") or {}).get("option") or []
    out = []
    for o in options:
        if o.get("option_type") != "put":
            continue
        delta = (o.get("greeks") or {}).get("delta")
        if delta is None:
            continue
        out.append(OptionQuote(strike=float(o["strike"]), delta=abs(float(delta)),
                               bid=float(o["bid"]), ask=float(o["ask"])))
    return out
```

- [ ] **Step 4: Run, expect pass** — 3 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/feeds.py bot/tests/test_feeds.py
git commit -m "feat: parse_chain (Tradier chain -> OptionQuote puts)"
```

---

### Task 3: parse_balances + parse_position_legs

**Files:** Modify `bot/app/feeds.py`, `bot/tests/test_feeds.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.app.feeds import parse_equity, parse_position_legs


def test_parse_equity():
    assert parse_equity({"balances": {"total_equity": 20123.45}}) == 20123.45


def test_parse_position_legs_maps_symbol_to_qty():
    resp = {"positions": {"position": [
        {"symbol": "SPY260619P00568000", "quantity": -2.0},
        {"symbol": "SPY260619P00558000", "quantity": 2.0},
    ]}}
    assert parse_position_legs(resp) == {"SPY260619P00568000": -2, "SPY260619P00558000": 2}


def test_parse_position_legs_handles_no_positions():
    assert parse_position_legs({"positions": "null"}) == {}
    # Tradier returns a single object (not a list) when exactly one position exists
    assert parse_position_legs({"positions": {"position": {"symbol": "X", "quantity": 1.0}}}) == {"X": 1}
```

- [ ] **Step 2: Run, expect fail** — `ImportError: parse_equity`.

- [ ] **Step 3: Implement** (append):
```python
def parse_equity(resp) -> float:
    return float(resp["balances"]["total_equity"])


def parse_position_legs(resp) -> dict:
    """Tradier positions JSON -> {occ_symbol: int qty}. Handles 'null' and single-object cases."""
    positions = (resp.get("positions") or {})
    if positions in (None, "null"):
        return {}
    items = positions.get("position")
    if not items:
        return {}
    if isinstance(items, dict):       # Tradier returns a bare object for a single position
        items = [items]
    return {p["symbol"]: int(p["quantity"]) for p in items}
```

- [ ] **Step 4: Run, expect pass** — 6 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/feeds.py bot/tests/test_feeds.py
git commit -m "feat: parse_equity + parse_position_legs"
```

---

### Task 4: compute_atr + vix_regime

**Files:** Modify `bot/app/feeds.py`, `bot/tests/test_feeds.py`

- [ ] **Step 1: Add failing tests** (append):
```python
from bot.app.feeds import compute_atr, vix_regime


def test_compute_atr_simple():
    # 3 bars, each true range = high-low = 2.0 (no gaps) -> ATR(2) = 2.0
    bars = [{"high": 10, "low": 8, "close": 9},
            {"high": 11, "low": 9, "close": 10},
            {"high": 12, "low": 10, "close": 11}]
    assert compute_atr(bars, n=2) == 2.0


def test_vix_regime_pct_rank_and_change():
    series = [10, 12, 14, 16, 20]   # latest 20 is the max -> pct_rank 1.0; change 20/16-1 = 0.25
    pct_rank, change = vix_regime(series)
    assert pct_rank == 1.0
    assert round(change, 4) == 0.25
```

- [ ] **Step 2: Run, expect fail** — `ImportError: compute_atr`.

- [ ] **Step 3: Implement** (append):
```python
def compute_atr(bars, n: int = 14) -> float:
    """ATR over the last n bars. bars: list of {high, low, close} oldest->newest."""
    trs = []
    prev_close = None
    for b in bars:
        h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    window = trs[-n:]
    return round(sum(window) / len(window), 4)


def vix_regime(vix_series):
    """Return (pct_rank, 1-day change) of the latest VIX vs the series. pct_rank in [0,1]."""
    s = list(vix_series)
    latest = s[-1]
    pct_rank = sum(1 for x in s if x <= latest) / len(s)
    change = (latest / s[-2] - 1.0) if len(s) >= 2 and s[-2] else 0.0
    return round(pct_rank, 4), round(change, 4)
```

- [ ] **Step 4: Run, expect pass** — 8 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/feeds.py bot/tests/test_feeds.py
git commit -m "feat: compute_atr + vix_regime"
```

---

### Task 5: reconcile_live (leg-aware broker reconcile)

**Files:** Create `bot/app/wiring.py`, `bot/tests/test_wiring.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_wiring.py`:
```python
from bot.app.wiring import reconcile_live
from bot.strategy.manage import ManagedPosition


def _pos(qty=2):
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=qty, expiry="2026-06-19")


def test_reconcile_live_clean_match():
    legs = {"SPY260619P00568000": -2, "SPY260619P00558000": 2}   # short -2, long +2
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.should_halt() is False


def test_reconcile_live_qty_mismatch_halts():
    legs = {"SPY260619P00568000": -1, "SPY260619P00558000": 1}   # broker only 1 lot, bot tracks 2
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.should_halt() is True


def test_reconcile_live_phantom_halts():
    r = reconcile_live([_pos(2)], {}, bot_equity=20_000.0, broker_equity=20_000.0)  # broker flat
    assert len(r.missing_at_broker) == 1 and r.should_halt() is True


def test_reconcile_live_untracked_leg_halts():
    legs = {"SPY260619P00568000": -2, "SPY260619P00558000": 2, "AAPL260619P00250000": -3}
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(r.untracked_at_broker) == 1 and r.should_halt() is True
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.app.wiring`.

- [ ] **Step 3: Implement** — `bot/app/wiring.py`:
```python
"""Production wiring: build Deps from Tradier, leg-aware reconcile, and the runner (spec §6,§7,§8)."""
from bot.app import feeds
from bot.app.orchestrator import Deps, tick
from bot.ops.ledger import DriftReport
from bot.strategy.s2b import _occ


def _legs_of(pos):
    """Expected (occ_symbol, signed_qty) for a bull put spread: short put -qty, long put +qty."""
    short = _occ(pos.ticker, pos.expiry, "P", pos.short_strike)
    long = _occ(pos.ticker, pos.expiry, "P", pos.long_strike)
    return [(short, -pos.qty), (long, +pos.qty)]


def reconcile_live(tracked, leg_map, bot_equity, broker_equity) -> DriftReport:
    """Verify each tracked spread's legs are present at the broker at the expected qty; flag the rest."""
    referenced = set()
    missing, qty_mismatch = [], []
    for p in tracked:
        ok = True
        for occ, signed_qty in _legs_of(p):
            referenced.add(occ)
            if occ not in leg_map:
                ok = False
            elif leg_map[occ] != signed_qty:
                qty_mismatch.append((p, occ))
                ok = False
        if not ok and not any(q[0] is p for q in qty_mismatch):
            missing.append(p)
    untracked = [occ for occ, q in leg_map.items() if occ not in referenced and q != 0]
    return DriftReport(missing_at_broker=missing, untracked_at_broker=untracked,
                       qty_mismatch=qty_mismatch, equity_drift=round(bot_equity - broker_equity, 2))
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_wiring.py -v` → 4 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/app/wiring.py bot/tests/test_wiring.py
git commit -m "feat: reconcile_live (leg-aware broker reconcile)"
```

---

### Task 6: runner loop + a smoke tick through real adapters

**Files:** Modify `bot/app/wiring.py`, `bot/tests/test_wiring.py`

- [ ] **Step 1: Add failing tests** (append to `bot/tests/test_wiring.py`):
```python
from bot.app.wiring import runner
from bot.app.orchestrator import BotState


def test_runner_calls_tick_n_times_then_stops():
    calls = {"n": 0}

    def fake_tick(state, deps, now):
        calls["n"] += 1
        return state

    clock = {"t": 0}
    def now_fn(): return clock["t"]
    def sleep_fn(s): clock["t"] += s

    runner(BotState(), deps=None, now_fn=now_fn, sleep_fn=sleep_fn,
           poll_s=60, ticks=3, tick_fn=fake_tick)
    assert calls["n"] == 3


def test_runner_stops_on_halt():
    def halting_tick(state, deps, now):
        state.halted = True
        return state
    calls = {"n": 0}
    def counting_tick(state, deps, now):
        calls["n"] += 1
        return halting_tick(state, deps, now)

    runner(BotState(), deps=None, now_fn=lambda: 0, sleep_fn=lambda s: None,
           poll_s=60, ticks=10, tick_fn=counting_tick)
    assert calls["n"] == 1   # halted after first tick -> stop
```

- [ ] **Step 2: Run, expect fail** — `ImportError: runner`.

- [ ] **Step 3: Implement** (append to `bot/app/wiring.py`):
```python
def runner(state, deps, now_fn, sleep_fn, poll_s, ticks, tick_fn=tick):
    """Call tick_fn every poll_s up to `ticks` times; stop early if the bot halts.
    now_fn/sleep_fn are injected so this is testable; production passes an ET clock + time.sleep."""
    for _ in range(ticks):
        state = tick_fn(state, deps, now_fn())
        if state.halted:
            break
        sleep_fn(poll_s)
    return state
```

- [ ] **Step 4: Run, expect pass** — 6 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/app/wiring.py bot/tests/test_wiring.py
git commit -m "feat: runner loop (testable; halts stop the loop)"
```

---

### Task 7: build_deps assembly + full-stack smoke + run the FULL suite

**Files:** Modify `bot/app/wiring.py`, `bot/tests/test_wiring.py`

- [ ] **Step 1: Add the failing test** (append) — drives a full `tick` through real adapters with a fake `http`:
```python
from bot.app.wiring import build_deps
from datetime import datetime


def _fake_http(responses):
    def http(method, path, params=None, data=None):
        for key, resp in responses.items():
            if key in path:
                return resp
        if path.endswith("/orders"):
            return {"order": {"id": 1, "status": "ok"}}
        return {}
    return http


def test_build_deps_drives_a_clean_monday_entry_tick():
    chain = {"options": {"option": [
        {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
        {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
        {"strike": 560.0, "option_type": "put", "bid": 2.00, "ask": 2.10, "greeks": {"delta": -0.22}},
        {"strike": 565.0, "option_type": "put", "bid": 2.80, "ask": 2.90, "greeks": {"delta": -0.30}},
    ]}}
    http = _fake_http({
        "/markets/options/chains": chain,
        "/balances": {"balances": {"total_equity": 20_000.0}},
        "/positions": {"positions": "null"},
        "/markets/quotes": {"quotes": {"quote": {"symbol": "SPY", "bid": 574.9, "ask": 575.1, "last": 575.0}}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    state = tick(BotState(), deps, datetime(2026, 6, 15, 10, 5))   # Monday 10:05
    assert len(state.open_positions) == 1 and state.open_positions[0].short_strike == 568.0
```

- [ ] **Step 2: Run, expect fail** — `ImportError: build_deps`.

- [ ] **Step 3: Implement** (append to `bot/app/wiring.py`):
```python
from bot.broker.tradier import TradierClient
from bot.broker.submit import submit_and_verify
from bot.strategy.manage import spread_value_mid, dte_from_expiry, build_close_payload
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
import time


def build_deps(http, account_id, get_spot, get_atr, get_vix_regime,
               poll_s=2, timeout_s=30, base_risk_pct=0.10):
    """Assemble a production Deps from a Tradier http callable + injected market-data feeds."""
    client = TradierClient(account_id=account_id, http=http)

    def get_chain(symbol, expiry):
        resp = http("GET", "/markets/options/chains",
                    params={"symbol": symbol, "expiration": expiry, "greeks": "true"})
        return feeds.parse_chain(resp)

    def _quote(symbol):
        q = http("GET", "/markets/quotes", params={"symbols": symbol})["quotes"]["quote"]
        return OptionQuote(0.0, 0.0, float(q["bid"]), float(q["ask"]))

    def mark_position(pos):
        short = _quote(_occ(pos.ticker, pos.expiry, "P", pos.short_strike))
        long = _quote(_occ(pos.ticker, pos.expiry, "P", pos.long_strike))
        return spread_value_mid(short, long)

    def broker_legs():
        return feeds.parse_position_legs(http("GET", f"/accounts/{account_id}/positions"))

    def broker_equity():
        return feeds.parse_equity(http("GET", f"/accounts/{account_id}/balances"))

    def open_spread(payload):
        state, _ = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return state.value

    def close_spread(pos, action):
        payload = build_close_payload(pos, limit_price=mark_position(pos))
        state, _ = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return state.value

    def account_state(today, concurrent):
        eq = broker_equity()
        return AccountState(eq, eq, 0.0, concurrent, 0.0, {}, today)

    return Deps(
        get_spot=get_spot, get_atr=get_atr, get_chain=get_chain,
        pick_expiry=lambda today: feeds.pick_weekly_expiry(today),
        get_vix_regime=get_vix_regime, account_state=account_state,
        mark_position=mark_position, dte_of=lambda p, today: dte_from_expiry(p.expiry, today),
        open_spread=open_spread, close_spread=close_spread,
        broker_positions=lambda: [],            # reconcile uses reconcile_live(broker_legs()) in the runner
        broker_equity=broker_equity, bot_equity=lambda: broker_equity(),
        alert_sink=lambda alerts: [print(f"[ALERT] {a.severity.value}: {a.message}") for a in alerts],
        base_risk_pct=base_risk_pct,
    )
```
> Note: `broker_positions` returns `[]` in `Deps` because the live reconcile path uses `reconcile_live(state.open_positions, broker_legs(), ...)` (leg-aware) — wired in the production runner, which is a thin documented entrypoint. The default `Deps.broker_positions` is only exercised by the in-memory orchestrator tests.

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_wiring.py -v` → 7 passed.

- [ ] **Step 5: Run the FULL suite** — `python -m pytest` → expected 109 (Plans 1–6) + 15 (Plan 7) = 124 passed.

- [ ] **Step 6: Commit**
```bash
git add bot/app/wiring.py bot/tests/test_wiring.py
git commit -m "feat: build_deps (full Tradier-wired Deps) + full-stack smoke tick"
```

---

## Self-review notes (done)
- **Spec coverage:** §3/§5 entry inputs (chain/spot/atr/vix/expiry) → Tasks 1–2,4; §7 reconcile (leg-aware) → Task 5; §6 env creds (via `make_http_from_env`, used by the entrypoint) → noted; runner → Task 6; full assembly → Task 7.
- **No placeholders:** complete code + expected counts. `broker_positions=lambda: []` in `build_deps` is documented (live path uses `reconcile_live`).
- **Type consistency:** `parse_chain`→`OptionQuote` (Plan 3 type); `reconcile_live`→`DriftReport` (Plan 5 type, incl. `qty_mismatch`); `build_deps`→`Deps` (Plan 6); `runner` defaults `tick_fn=tick` (Plan 6).
- **Testability honesty:** every adapter is pure/fixture-tested; the only untested code is the production entrypoint that reads env + passes `time.time`/`time.sleep`/an ET clock — documented below, not unit-tested by design.

## Final go-live entrypoint (thin, NOT unit-tested — run manually against sandbox)
A `__main__` block (or `ops/run_s2b.py`) that: (1) `http = make_http_from_env()` (TRADIER_TOKEN, TRADIER_BASE_URL=sandbox); (2) build `get_spot`/`get_atr` from a `/markets/history` daily pull + `compute_atr`, and `get_vix_regime` from a VIX series; (3) `deps = build_deps(http, account_id, ...)`; (4) loop: `drift = reconcile_live(state.open_positions, broker_legs(), bot_eq, broker_eq)`; if `drift.should_halt()` alert+halt; else `state = tick(state, deps, now_et())`; `sleep(poll_s)`. **Run against sandbox first; then the spec §8 chaos drills on the live binary and the paper→live gates before any real money.**
```
