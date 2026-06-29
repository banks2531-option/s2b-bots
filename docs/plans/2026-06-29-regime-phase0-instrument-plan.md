# Regime Engine — Phase 0 (Instrument) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Compute a `RegimeState` every tick from all available market context (VIX term structure,
SPY trend/ATR, position concentration, equity drawdown, Unusual Whales flow) and **shadow-log it
next to bot activity — with ZERO change to trading behavior** — so we build the labeled
regime→outcome dataset that calibrates Phase 1.

**Architecture:** New `bot/regime/` package of small, independently-tested pure adapters, assembled
by an `engine` into a `RegimeState`, scored by a `scorer` (derived/decision fields computed for
logging only). A `regime_log` CSV sink records one row per tick. The orchestrator computes + logs
the state inside a **try/except that can never raise**, then proceeds exactly as before — no gate,
no size change, no de-gross. The decision fields are recorded but **not applied** in Phase 0.

**Tech Stack:** Python 3.11, pytest, dataclasses, injected dependencies (matches existing `bot/`
conventions). VIX via yfinance (already used by `fetch_vix_regime`). UW via REST (graceful-degrade if
unavailable). No new behavior, no new live risk.

**Non-negotiable invariant (every task must preserve it):** *Phase 0 changes no trading decision.*
Entry/exit/sizing/halting are byte-identical to today. Regime computation failure must be swallowed
and logged, never propagated.

---

## File Structure

- Create: `bot/regime/__init__.py`
- Create: `bot/regime/state.py` — `RegimeState` dataclass (raw context + derived/decision fields)
- Create: `bot/regime/vix_term.py` — VIX/VIX3M term-structure pure calcs + fetch
- Create: `bot/regime/trend.py` — SPY trend bias + atr_pct from daily bars
- Create: `bot/regime/concentration.py` — exposure map + concentration score from open positions
- Create: `bot/regime/equity_dd.py` — drawdown-from-peak from an equity series
- Create: `bot/regime/flow_uw.py` — Unusual Whales client + flow_bias/flow_extreme (graceful-degrade)
- Create: `bot/regime/scorer.py` — derived fields (stress_score, size_multiplier, pause, degross, kill)
- Create: `bot/regime/engine.py` — `compute_regime_state(...)` assembling adapters → `RegimeState`
- Create: `bot/regime/logger.py` — `make_regime_logger(path)` CSV sink
- Modify: `bot/app/orchestrator.py` — compute + log regime in `tick()` (guarded, no behavior change)
- Modify: `bot/app/orchestrator.py` `Deps` — add optional `regime_provider` + `regime_log` (defaulted to no-ops)
- Modify: `bot/app/wiring.py` — wire real adapters + logger into `Deps`
- Modify: `bot/app/run_s2b.py` — pass a `regime_<tag>.csv` path
- Tests under `bot/tests/` mirroring each module.

---

## Task 1: `RegimeState` dataclass

**Files:**
- Create: `bot/regime/__init__.py`
- Create: `bot/regime/state.py`
- Test: `bot/tests/test_regime_state.py`

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_state.py
from dataclasses import asdict
from bot.regime.state import RegimeState


def test_regime_state_defaults_and_serializable():
    s = RegimeState()
    # raw context defaults are "unknown/neutral", decision fields are no-op (full size, no pause)
    assert s.trend_bias == "neutral"
    assert s.flow_bias == "neutral"
    assert s.flow_extreme is False
    assert s.size_multiplier == 1.0
    assert s.pause_entries is False
    assert s.degross_target == 0.0
    assert s.kill is False
    d = asdict(s)                      # must be flat-serializable for CSV logging
    assert "stress_score" in d and "vix_term_slope" in d
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest bot/tests/test_regime_state.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/__init__.py
"""Regime & Context Engine (Phase 0: instrument only — no trading behavior change)."""
```

```python
# bot/regime/state.py
"""The single shared market-state object, recomputed each tick. Phase 0 logs it; it does NOT
gate any trading decision yet (decision fields default to no-ops so they are inert if ever read)."""
from dataclasses import dataclass


@dataclass
class RegimeState:
    # ── raw context (None/"neutral" = unknown, so a missing feed is never mistaken for a signal) ──
    vix_level: float = None
    vix_pct_rank: float = None        # vs trailing ~120 trading days
    vix_term_slope: float = None      # VIX/VIX3M - 1 ; < 0 = backwardation = stress
    atr_pct: float = None             # ATR(14)/spot
    trend_bias: str = "neutral"       # "up" | "down" | "neutral"
    equity_drawdown: float = 0.0      # fraction below the bot's own equity peak (>= 0)
    concentration: float = 0.0        # 0..1, higher = more clustered book
    flow_bias: str = "neutral"        # UW: "bullish" | "bearish" | "neutral"
    flow_extreme: bool = False        # UW: blow-out flow/skew -> risk-off veto
    # ── derived / decision (computed for LOGGING in Phase 0; NOT applied to trading) ──
    stress_score: float = 0.0         # 0..1 composite
    risk_on: bool = True
    size_multiplier: float = 1.0      # 0..1 -> would scale base_risk_pct in Phase 1
    pause_entries: bool = False
    widen_cushion_atr: float = 0.0
    degross_target: float = 0.0       # 0..1 fraction of book to trim
    kill: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest bot/tests/test_regime_state.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/__init__.py bot/regime/state.py bot/tests/test_regime_state.py
git commit -m "regime: RegimeState dataclass (Phase 0 instrument)"
```

---

## Task 2: VIX term-structure adapter

**Files:**
- Create: `bot/regime/vix_term.py`
- Test: `bot/tests/test_regime_vix_term.py`

Pure calculations only; the network fetch is a thin separate function kept out of the unit test.

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_vix_term.py
from bot.regime.vix_term import term_slope, pct_rank


def test_term_slope_backwardation_is_negative():
    # front VIX above 3M = stress/backwardation -> negative slope
    assert term_slope(22.0, 20.0) < 0
    # contango (front below 3M) -> positive
    assert term_slope(18.0, 20.0) > 0
    assert term_slope(20.0, 0.0) == 0.0          # guard divide-by-zero


def test_pct_rank_basic():
    series = [10, 12, 14, 16, 18, 20]
    assert pct_rank(20, series) == 1.0           # at the top
    assert pct_rank(10, series) <= 0.34          # near the bottom
    assert pct_rank(99, []) is None              # empty -> unknown
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_vix_term.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/vix_term.py
"""VIX term-structure context. term_slope < 0 (front > 3M, backwardation) signals stress.
Pure calcs are unit-tested; fetch_vix_term() is thin network glue (yfinance) used in wiring."""


def term_slope(vix_front: float, vix_3m: float) -> float:
    """(front / 3M) - 1. Negative = backwardation (stress). 0.0 if 3M is missing/zero."""
    if not vix_3m:
        return 0.0
    return round(vix_front / vix_3m - 1.0, 4)


def pct_rank(latest: float, series) -> float:
    """Fraction of `series` <= latest, in [0,1]. None if series is empty (unknown)."""
    s = list(series)
    if not s:
        return None
    return round(sum(1 for x in s if x <= latest) / len(s), 4)


def fetch_vix_term(lookback=120):
    """(vix_level, vix_pct_rank, vix_term_slope) from yfinance ^VIX and ^VIX3M. Network glue;
    returns (None, None, None) on any failure so the engine degrades gracefully (no raise)."""
    try:
        import yfinance as yf
        def closes(sym):
            c = yf.download(sym, period="6mo", progress=False, auto_adjust=False)["Close"]
            if hasattr(c, "columns"):
                c = c.iloc[:, 0]
            return [float(x) for x in c.dropna().values]
        vix = closes("^VIX")
        v3m = closes("^VIX3M")
        if not vix:
            return (None, None, None)
        level = vix[-1]
        rank = pct_rank(level, vix[-lookback:])
        slope = term_slope(level, v3m[-1]) if v3m else 0.0
        return (round(level, 2), rank, slope)
    except Exception:
        return (None, None, None)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_vix_term.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/vix_term.py bot/tests/test_regime_vix_term.py
git commit -m "regime: VIX term-structure adapter"
```

---

## Task 3: Trend / ATR adapter

**Files:**
- Create: `bot/regime/trend.py`
- Test: `bot/tests/test_regime_trend.py`

Reuses the existing `feeds.compute_atr` bar format `[{high,low,close}]` (oldest→newest).

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_trend.py
from bot.regime.trend import trend_bias, atr_pct


def _bars(closes):
    return [{"high": c + 1, "low": c - 1, "close": c} for c in closes]


def test_trend_bias_up_down_neutral():
    up = _bars(list(range(100, 160)))            # steadily rising, price well above MAs
    assert trend_bias(up) == "up"
    down = _bars(list(range(160, 100, -1)))      # steadily falling
    assert trend_bias(down) == "down"
    flat = _bars([130] * 60)
    assert trend_bias(flat) == "neutral"
    assert trend_bias([]) == "neutral"           # no data -> unknown/neutral


def test_atr_pct_positive_fraction():
    bars = _bars([100 + i for i in range(30)])
    p = atr_pct(bars)
    assert p is not None and 0 < p < 0.5
    assert atr_pct([]) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_trend.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/trend.py
"""SPY trend context from daily bars (same [{high,low,close}] format as feeds.compute_atr).
trend_bias compares the latest close to its 20- and 50-day SMAs; atr_pct = ATR(14)/close."""
from bot.app.feeds import compute_atr


def _sma(closes, n):
    w = closes[-n:]
    return sum(w) / len(w) if w else None


def trend_bias(bars, short_n=20, long_n=50, band=0.001) -> str:
    """'up' if close is above both SMAs, 'down' if below both, else 'neutral'. 'neutral' if too
    few bars. `band` ignores ties within 0.1% to avoid flip-flopping on flat tape."""
    closes = [float(b["close"]) for b in bars]
    if len(closes) < short_n:
        return "neutral"
    c = closes[-1]
    s = _sma(closes, short_n)
    l = _sma(closes, long_n) if len(closes) >= long_n else s
    if c > s * (1 + band) and c > l * (1 + band):
        return "up"
    if c < s * (1 - band) and c < l * (1 - band):
        return "down"
    return "neutral"


def atr_pct(bars, n=14):
    """ATR(n)/latest close as a fraction. None if no bars."""
    if not bars:
        return None
    atr = compute_atr(bars, n=n)
    c = float(bars[-1]["close"])
    return round(atr / c, 4) if c else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_trend.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/trend.py bot/tests/test_regime_trend.py
git commit -m "regime: SPY trend/ATR adapter"
```

---

## Task 4: Concentration adapter

**Files:**
- Create: `bot/regime/concentration.py`
- Test: `bot/tests/test_regime_concentration.py`

Operates on `ManagedPosition` objects (`bot.strategy.manage.ManagedPosition`).

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_concentration.py
from bot.regime.concentration import concentration_score, exposure_map
from bot.strategy.manage import ManagedPosition


def _p(short, expiry):
    return ManagedPosition("SPY", short, short - 10, credit=1.4, qty=7, expiry=expiry)


def test_concentration_high_when_book_is_identical():
    # 5 near-identical spreads (same expiry, tight strike band) -> high concentration
    book = [_p(727, "2026-07-10"), _p(728, "2026-07-10"), _p(727, "2026-07-10"),
            _p(728, "2026-07-10"), _p(728, "2026-07-10")]
    assert concentration_score(book) > 0.8


def test_concentration_low_when_spread_out():
    book = [_p(700, "2026-07-10"), _p(720, "2026-07-17"), _p(740, "2026-07-24")]
    assert concentration_score(book) < 0.5
    assert concentration_score([]) == 0.0


def test_exposure_map_groups_by_expiry_and_band():
    book = [_p(727, "2026-07-10"), _p(728, "2026-07-10")]
    m = exposure_map(book, band=3.0)
    assert m[("2026-07-10", 726.0)] == 2          # both fall in the same $3 band (floor to 3)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_concentration.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/concentration.py
"""Book concentration context: how clustered open positions are by expiry + strike band.
Used in Phase 1 to cap correlated exposure; in Phase 0 only logged."""
from collections import Counter


def exposure_map(positions, band: float = 3.0) -> dict:
    """{(expiry, banded_short_strike): count}. Strikes are floored to `band`-wide buckets so
    near-identical spreads (e.g. 727 & 728 in a $3 band) group together."""
    m = Counter()
    for p in positions:
        bucket = (float(p.short_strike) // band) * band
        m[(p.expiry, bucket)] += 1
    return dict(m)


def concentration_score(positions) -> float:
    """0..1: 1 - (distinct expiry/band buckets / positions). 0 when every position is unique,
    approaches 1 when all positions pile into one bucket. 0.0 for an empty book."""
    n = len(positions)
    if n == 0:
        return 0.0
    buckets = len(exposure_map(positions))
    return round(1.0 - buckets / n, 4)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_concentration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/concentration.py bot/tests/test_regime_concentration.py
git commit -m "regime: book concentration adapter"
```

---

## Task 5: Equity drawdown adapter

**Files:**
- Create: `bot/regime/equity_dd.py`
- Test: `bot/tests/test_regime_equity_dd.py`

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_equity_dd.py
from bot.regime.equity_dd import drawdown_from_peak


def test_drawdown_from_peak():
    assert drawdown_from_peak(110, peak=110) == 0.0          # at a new high
    assert round(drawdown_from_peak(99, peak=110), 4) == 0.1  # 10% below peak
    assert drawdown_from_peak(120, peak=110) == 0.0          # new high resets to 0
    assert drawdown_from_peak(100, peak=0) == 0.0            # no peak yet -> 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_equity_dd.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/equity_dd.py
"""Own-equity drawdown context. drawdown_from_peak returns the fraction below the running peak
(>= 0). Phase 1 uses this for the de-gross/kill thresholds; Phase 0 only logs it."""


def drawdown_from_peak(equity: float, peak: float) -> float:
    """max(0, (peak - equity) / peak). 0.0 if peak <= 0 (no history yet) or at/above peak."""
    if not peak or peak <= 0:
        return 0.0
    return round(max(0.0, (peak - equity) / peak), 4)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_equity_dd.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/equity_dd.py bot/tests/test_regime_equity_dd.py
git commit -m "regime: equity drawdown adapter"
```

---

## Task 6: Unusual Whales flow adapter (graceful-degrade)

**Files:**
- Create: `bot/regime/flow_uw.py`
- Test: `bot/tests/test_regime_flow_uw.py`

Phase 0 goal for UW: **pull market-wide flow context and never break the bot if UW is unavailable.**
The HTTP call is injected so the unit test is hermetic. Exact endpoint paths are confirmed against
the live key during deploy (Task 9); the parser is written against UW's documented market-tide shape
and is the only thing unit-tested here.

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_flow_uw.py
from bot.regime.flow_uw import parse_market_tide, flow_context


def test_parse_market_tide_bullish_bearish():
    # net call premium dominating -> bullish; net put premium dominating -> bearish
    assert parse_market_tide({"data": [{"net_call_premium": 9e8, "net_put_premium": 1e8}]}) == ("bullish", False)
    assert parse_market_tide({"data": [{"net_call_premium": 1e8, "net_put_premium": 9e8}]}) == ("bearish", False)
    # extreme one-sided put premium -> bearish AND extreme veto
    bias, extreme = parse_market_tide({"data": [{"net_call_premium": 1e7, "net_put_premium": 2e9}]})
    assert bias == "bearish" and extreme is True


def test_flow_context_degrades_gracefully_on_error():
    def boom(*a, **k):
        raise ConnectionError("UW down")
    # any failure -> neutral/False, never raises (Phase 0 must not break the bot)
    assert flow_context(http=boom) == ("neutral", False)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_flow_uw.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/flow_uw.py
"""Unusual Whales market-wide flow context. Phase 0: derive a coarse bias + an 'extreme' veto flag
from net option premium; degrade to ('neutral', False) on ANY error so the bot never breaks.
The exact endpoint is confirmed against the live key in wiring; `http` is injected for testing."""

EXTREME_RATIO = 5.0          # one side >= 5x the other -> blow-out flow (risk-off veto candidate)


def parse_market_tide(payload) -> tuple:
    """UW market-tide-shaped payload -> (flow_bias, flow_extreme).
    bias: 'bullish' if net call premium > net put premium, else 'bearish' (ties -> 'neutral').
    extreme: True when the dominant side is >= EXTREME_RATIO times the other."""
    rows = (payload or {}).get("data") or []
    if not rows:
        return ("neutral", False)
    r = rows[-1]                                   # most recent bucket
    call_p = float(r.get("net_call_premium") or 0)
    put_p = float(r.get("net_put_premium") or 0)
    if call_p == put_p:
        return ("neutral", False)
    bias = "bullish" if call_p > put_p else "bearish"
    hi, lo = max(call_p, put_p), max(min(call_p, put_p), 1.0)
    return (bias, hi / lo >= EXTREME_RATIO)


def flow_context(http, path="/api/market/market-tide") -> tuple:
    """Fetch + parse market tide. `http` is a callable (method, path)->json (UW REST).
    Returns ('neutral', False) on any failure — Phase 0 must never propagate a UW error."""
    try:
        return parse_market_tide(http("GET", path))
    except Exception:
        return ("neutral", False)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_flow_uw.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/flow_uw.py bot/tests/test_regime_flow_uw.py
git commit -m "regime: Unusual Whales flow adapter (graceful-degrade)"
```

---

## Task 7: Scorer (derived fields — computed for logging only)

**Files:**
- Create: `bot/regime/scorer.py`
- Test: `bot/tests/test_regime_scorer.py`

Computes the composite `stress_score` and the would-be decision fields using the §6 best-judgment
defaults. **Phase 0 logs these; nothing applies them.** Centralising them now means Phase 1 only has
to wire the orchestrator to read fields that are already validated against logged data.

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_scorer.py
from bot.regime.state import RegimeState
from bot.regime.scorer import score


def test_score_calm_is_full_size_no_action():
    s = score(RegimeState(vix_pct_rank=0.3, vix_term_slope=0.05, trend_bias="up",
                          equity_drawdown=0.0, flow_extreme=False))
    assert s.size_multiplier == 1.0 and s.pause_entries is False
    assert s.degross_target == 0.0 and s.kill is False and s.risk_on is True


def test_score_stress_scales_and_pauses():
    s = score(RegimeState(vix_pct_rank=0.95, vix_term_slope=-0.08, trend_bias="down",
                          equity_drawdown=0.0, flow_extreme=True))
    assert s.size_multiplier < 1.0
    assert s.pause_entries is True            # risk-off composite
    assert s.stress_score > 0.5


def test_score_kill_and_degross_thresholds():
    # -6% drawdown -> de-gross + pause ; -10% -> kill + flat
    s6 = score(RegimeState(equity_drawdown=0.06))
    assert s6.degross_target >= 0.5 and s6.pause_entries is True and s6.kill is False
    s10 = score(RegimeState(equity_drawdown=0.10))
    assert s10.kill is True and s10.degross_target == 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_scorer.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/scorer.py
"""Map raw RegimeState context -> derived/decision fields using the scoped §6 defaults.
Phase 0: these fields are LOGGED ONLY and never applied to trading. Pure + deterministic so the
thresholds can be re-tuned against the logged regime->outcome dataset before Phase 1 turns them on."""
from dataclasses import replace
from bot.regime.state import RegimeState

# §6 best-judgment thresholds (as fractions of equity / unit-free)
DD_DEGROSS = 0.06          # -6% from peak -> de-gross + pause
DD_KILL = 0.10             # -10% from peak -> kill + flat
VIX_RANK_STRESS = 0.80     # top-20% VIX
TERM_BACKWARDATION = 0.0   # slope < 0 = stress


def _stress_components(s: RegimeState):
    c = []
    if s.vix_pct_rank is not None:
        c.append(min(1.0, max(0.0, (s.vix_pct_rank - 0.5) / 0.5)))   # 0 at median, 1 at top
    if s.vix_term_slope is not None and s.vix_term_slope < TERM_BACKWARDATION:
        c.append(min(1.0, abs(s.vix_term_slope) / 0.10))             # backwardation depth
    if s.trend_bias == "down":
        c.append(1.0)
    if s.flow_extreme:
        c.append(1.0)
    return c


def score(s: RegimeState) -> RegimeState:
    """Return a copy of `s` with derived fields filled per the scoped defaults."""
    comps = _stress_components(s)
    stress = round(sum(comps) / len(comps), 4) if comps else 0.0

    kill = s.equity_drawdown >= DD_KILL
    degross = 1.0 if kill else (0.5 if s.equity_drawdown >= DD_DEGROSS else 0.0)
    # size dial: full size when calm, down to 0.5 at high stress
    size_mult = round(1.0 - 0.5 * stress, 4)
    pause = bool(kill or degross > 0 or stress >= 0.6 or s.flow_extreme)
    widen = round(0.5 * stress, 4)        # add up to +0.5 ATR cushion under stress

    return replace(s,
        stress_score=stress,
        risk_on=not pause,
        size_multiplier=size_mult,
        pause_entries=pause,
        widen_cushion_atr=widen,
        degross_target=degross,
        kill=kill)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_scorer.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/scorer.py bot/tests/test_regime_scorer.py
git commit -m "regime: scorer (derived fields, logged-only in Phase 0)"
```

---

## Task 8: Engine + regime logger

**Files:**
- Create: `bot/regime/engine.py`
- Create: `bot/regime/logger.py`
- Test: `bot/tests/test_regime_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# bot/tests/test_regime_engine.py
from bot.regime.engine import compute_regime_state
from bot.regime.logger import make_regime_logger
import csv


def test_compute_regime_state_assembles_and_scores():
    bars = [{"high": c + 1, "low": c - 1, "close": c} for c in range(100, 160)]
    st = compute_regime_state(
        vix=(18.0, 0.4, 0.05), bars=bars, positions=[], equity=20000, equity_peak=20000,
        flow=("bullish", False))
    assert st.trend_bias == "up" and st.vix_level == 18.0
    assert st.size_multiplier == 1.0 and st.stress_score == 0.0   # scored, calm


def test_compute_regime_state_never_raises_on_bad_inputs():
    st = compute_regime_state(vix=(None, None, None), bars=[], positions=[],
                              equity=None, equity_peak=None, flow=("neutral", False))
    assert st.trend_bias == "neutral"     # degraded, not crashed


def test_make_regime_logger_writes_row(tmp_path):
    p = tmp_path / "regime.csv"
    log = make_regime_logger(str(p))
    from bot.regime.state import RegimeState
    log(RegimeState(vix_level=18.0), ts="2026-06-29T10:00", event="TICK")
    rows = list(csv.DictReader(open(p)))
    assert rows[0]["vix_level"] == "18.0" and rows[0]["event"] == "TICK"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_regime_engine.py -q`
Expected: FAIL.

- [ ] **Step 3: Write the implementation**

```python
# bot/regime/engine.py
"""Assemble a scored RegimeState from already-fetched inputs. Pure (no network) so it is fully
testable; the wiring layer fetches vix/bars/flow and passes them in. Never raises — bad inputs
degrade to a neutral state (Phase 0 must not break the bot)."""
from bot.regime.state import RegimeState
from bot.regime.trend import trend_bias, atr_pct
from bot.regime.concentration import concentration_score
from bot.regime.equity_dd import drawdown_from_peak
from bot.regime.scorer import score


def compute_regime_state(vix, bars, positions, equity, equity_peak, flow) -> RegimeState:
    """vix=(level,pct_rank,term_slope); bars=[{high,low,close}]; positions=[ManagedPosition];
    flow=(flow_bias, flow_extreme). Returns a scored RegimeState."""
    try:
        level, rank, slope = vix
        fbias, fextreme = flow
        raw = RegimeState(
            vix_level=level, vix_pct_rank=rank, vix_term_slope=slope,
            atr_pct=atr_pct(bars), trend_bias=trend_bias(bars),
            equity_drawdown=drawdown_from_peak(equity or 0, equity_peak or 0),
            concentration=concentration_score(positions),
            flow_bias=fbias, flow_extreme=bool(fextreme))
        return score(raw)
    except Exception:
        return RegimeState()        # neutral, inert — never propagate
```

```python
# bot/regime/logger.py
"""CSV sink for shadow-logging RegimeState each tick (Phase 0 dataset for Phase 1 calibration)."""
import csv as _csv
import os as _os
from dataclasses import asdict, fields

from bot.regime.state import RegimeState

_FIELDS = ["ts", "event"] + [f.name for f in fields(RegimeState)]


def make_regime_logger(path):
    """Return log(state, ts, event) that appends one flat row per call (header written once)."""
    def log(state: RegimeState, ts: str, event: str = "TICK"):
        exists = _os.path.exists(path)
        row = {"ts": ts, "event": event, **asdict(state)}
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)
    return log
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest bot/tests/test_regime_engine.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/regime/engine.py bot/regime/logger.py bot/tests/test_regime_engine.py
git commit -m "regime: engine assembler + CSV shadow-logger"
```

---

## Task 9: Orchestrator integration — compute + log, ZERO behavior change

**Files:**
- Modify: `bot/app/orchestrator.py` (`Deps` + `tick`)
- Test: `bot/tests/test_orchestrator.py` (add two tests)

The `Deps` gains two optional, defaulted-to-no-op hooks so every existing test/construction keeps
working unchanged: `regime_provider` (a callable `() -> RegimeState` or `None`) and `regime_log`
(a callable `(state, ts, event) -> None`). In `tick`, after the existing cycles run, compute + log
the regime inside a `try/except` that swallows everything. **No cycle reads the regime.**

- [ ] **Step 1: Write the failing tests**

```python
# add to bot/tests/test_orchestrator.py

def test_tick_logs_regime_without_changing_behavior():
    # regime logging must not alter the entry decision: clean Monday still enters exactly one.
    logged = []
    from bot.regime.state import RegimeState
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [], open_spread=lambda payload: "filled")
    d.regime_provider = lambda: RegimeState(vix_level=18.0)
    d.regime_log = lambda s, ts, event: logged.append((s, event))
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1            # behavior identical to test_tick_enters_on_clean_monday
    assert logged and logged[0][0].vix_level == 18.0  # and the regime was logged


def test_tick_regime_failure_never_breaks_tick():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [], open_spread=lambda payload: "filled")
    d.regime_provider = lambda: (_ for _ in ()).throw(RuntimeError("regime feed down"))
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1            # entry still happened; regime error swallowed
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest bot/tests/test_orchestrator.py -q -k "regime"`
Expected: FAIL (`Deps` has no `regime_provider`).

- [ ] **Step 3: Implement — add fields to `Deps` and the guarded block to `tick`**

In `bot/app/orchestrator.py`, add to the `Deps` dataclass (after `trade_log`):

```python
    regime_provider: callable = None                 # () -> RegimeState | None ; Phase 0: logged only
    regime_log: callable = (lambda state, ts, event: None)   # (RegimeState, ts, event) -> None
```

Replace the body of `tick` with (keeps the existing three cycles exactly; adds a guarded tail):

```python
def tick(state: BotState, deps: Deps, now) -> BotState:
    """One bot cycle: reconcile (may halt) -> manage open positions -> enter if eligible.
    PHASE 0: also compute + shadow-log the market regime. This MUST NOT change any decision above,
    so it runs last, inside a try/except that swallows everything (a regime fault never halts trading)."""
    today = now.strftime("%Y-%m-%d")
    state, reconcile_ok = run_reconcile_cycle(state, deps)
    state, _ = run_management_cycle(state, deps, today)   # ALWAYS runs (stops must fire)
    if reconcile_ok:
        state, _ = run_entry_cycle(state, deps, now)
    if deps.regime_provider is not None:                  # Phase 0 instrument: observe only
        try:
            rs = deps.regime_provider()
            if rs is not None:
                deps.regime_log(rs, now.isoformat(), "TICK")
        except Exception as exc:
            deps.alert_sink([Alert(Severity.INFO, f"regime log skipped: {exc}")])
    return state
```

- [ ] **Step 4: Run to verify they pass + full suite stays green**

Run: `python -m pytest bot/tests/test_orchestrator.py -q -k "regime"` → PASS
Run: `python -m pytest bot/tests/ -q` → all green (no existing test changed behavior)

- [ ] **Step 5: Commit**

```bash
git add bot/app/orchestrator.py bot/tests/test_orchestrator.py
git commit -m "regime: tick computes + shadow-logs RegimeState (no behavior change)"
```

---

## Task 10: Wiring — build real providers + confirm UW endpoint

**Files:**
- Modify: `bot/app/wiring.py` (`build_deps`)
- Modify: `bot/app/run_s2b.py` (`build_and_run` — pass `regime_<tag>.csv` path + UW token)
- Test: `bot/tests/test_wiring.py` (one integration test with fakes)

This is the only task that touches network glue. The UW token comes from the environment
(`UW_TOKEN`); if absent, the flow provider degrades to `("neutral", False)` and the bot is unaffected.

- [ ] **Step 1: Write the failing test**

```python
# add to bot/tests/test_wiring.py
def test_build_deps_attaches_regime_provider_that_returns_state():
    http = _fake_http({
        "/balances": {"balances": {"total_equity": 20000.0}},
        "/positions": {"positions": "null"},
        "/markets/history": {"history": {"day": [
            {"date": "2026-06-01", "high": 101, "low": 99, "close": 100}] * 30}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      regime_log=lambda s, ts, e: None)
    assert deps.regime_provider is not None
    rs = deps.regime_provider()           # must return a RegimeState, never raise
    from bot.regime.state import RegimeState
    assert isinstance(rs, RegimeState)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest bot/tests/test_wiring.py -q -k "regime"`
Expected: FAIL (`build_deps` has no `regime_log` param / no provider).

- [ ] **Step 3: Implement the provider in `build_deps`**

Add params `regime_log=(lambda s, ts, e: None)` and `uw_http=None` to `build_deps`. Before the
`return Deps(...)`, build a provider that fetches term-structure VIX, SPY daily bars, the book,
equity + running peak, and UW flow — each guarded:

```python
    from bot.regime.engine import compute_regime_state
    from bot.regime.vix_term import fetch_vix_term
    from bot.regime.flow_uw import flow_context

    _peak = {"v": 0.0}
    def regime_provider():
        try:
            eq = broker_equity()
            _peak["v"] = max(_peak["v"], eq or 0.0)
            today = __import__("datetime").date.today().strftime("%Y-%m-%d")
            start = (__import__("datetime").date.today()
                     - __import__("datetime").timedelta(days=90)).strftime("%Y-%m-%d")
            hist = http("GET", "/markets/history",
                        params={"symbol": "SPY", "interval": "daily", "start": start, "end": today})
            bars = feeds.parse_history(hist)
            flow = flow_context(uw_http) if uw_http is not None else ("neutral", False)
            return compute_regime_state(
                vix=fetch_vix_term(), bars=bars,
                positions=feeds.reconstruct_spreads(broker_legs()),
                equity=eq, equity_peak=_peak["v"], flow=flow)
        except Exception:
            from bot.regime.state import RegimeState
            return RegimeState()
```

Pass `regime_provider=regime_provider, regime_log=regime_log` into `Deps(...)`.

In `bot/app/run_s2b.py` `build_and_run`, add:

```python
    from bot.regime.logger import make_regime_logger
    regime_log = make_regime_logger(f"regime_{tag}.csv")
    # build a UW http callable from env if a token is present (else None -> neutral flow)
    uw_http = None
    uw_tok = os.environ.get("UW_TOKEN")
    if uw_tok:
        import urllib.request, json as _json
        def uw_http(method, path, params=None):
            url = "https://api.unusualwhales.com" + path
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {uw_tok}",
                                                       "Accept": "application/json"})
            return _json.load(urllib.request.urlopen(req, timeout=15))
```

…and pass `regime_log=regime_log, uw_http=uw_http` into `build_deps(...)`.

- [ ] **Step 4: Run to verify it passes + full suite green**

Run: `python -m pytest bot/tests/ -q` → all green.

- [ ] **Step 5: Confirm the UW endpoint against the live key (manual, documented)**

On the droplet (or locally with `UW_TOKEN` set), curl the market-tide endpoint and confirm the JSON
shape matches `parse_market_tide`. If UW's path/field names differ, adjust `flow_uw.py`'s `path` and
the `net_call_premium`/`net_put_premium` keys, re-run Task 6 tests. Record the confirmed endpoint in
`docs/specs/2026-06-29-regime-context-engine-scope.md` §1.3.

- [ ] **Step 6: Commit**

```bash
git add bot/app/wiring.py bot/app/run_s2b.py bot/tests/test_wiring.py
git commit -m "regime: wire live providers + UW flow (env token, graceful-degrade)"
```

---

## Task 11: Deploy Phase 0 to the droplet (observe-only) + verify

**Files:** none (deploy + verification)

- [ ] **Step 1:** scp the new `bot/regime/` package + modified `bot/app/{orchestrator,wiring,run_s2b}.py`
  to `/root/s2b-bot/...` (Bot B first).
- [ ] **Step 2:** (optional) add `UW_TOKEN=...` to `/root/s2b-bot/s2b.env`. If omitted, flow logs as neutral.
- [ ] **Step 3:** `pip install yfinance` already present; restart `s2b-alldays.service`.
- [ ] **Step 4:** confirm `regime_alldays.csv` begins accumulating rows and the bot's trading behavior
  is unchanged (open count, entries, no new halts). Verify a few rows have sane vix/trend/flow values.
- [ ] **Step 5:** let it run; after a few sessions, pull `regime_alldays.csv` for the calibration
  dataset that Phase 1 will use. **Do not enable any gating** — that is Phase 1, a separate plan.

---

## Self-Review

- **Spec coverage:** Phase 0 of the scope (§5) = "data adapters + shadow-log RegimeState next to every
  trade, no behavior change." Tasks 1–8 build every adapter named in scope §1.2 (VIX term, trend/ATR,
  concentration, equity DD, UW flow) + the engine/scorer/logger; Task 9 logs without gating; Tasks
  10–11 wire + deploy observe-only. UW is API-based per the user's access. ✓
- **Zero-behavior-change invariant:** enforced by Task 9 (regime runs last, guarded, read by nothing)
  and its two dedicated tests, plus "full suite green" gates in Tasks 9–10. ✓
- **Type consistency:** `RegimeState` fields are referenced identically across scorer/engine/logger;
  `compute_regime_state(vix, bars, positions, equity, equity_peak, flow)` signature matches its test
  and the wiring provider call. `flow_context(http, path)` and `parse_market_tide(payload)` match. ✓
- **No placeholders:** every code step contains complete, runnable code. UW endpoint confirmation
  (Task 10 Step 5) is an explicit manual verification, not a code placeholder. ✓
```
