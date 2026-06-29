# Regime & Context Engine — Program Scope

Date: 2026-06-29 · Status: SCOPE (not yet approved for build) · Author: Shawn + Claude

> Goal: make the S2b bots **logical, dynamic, and regime-aware** — not just mechanical theta
> harvesters — by inserting a market-state engine between data and decisions, using every resource
> available (VIX term structure, trend, ATR, **Unusual Whales API**, own-equity drawdown) to
> protect the book in stress and (only if it survives validation) to sharpen entries.

---

## 0. The reframe that governs the whole program

Two fundamentally different capabilities, with opposite track records in this project:

- **Protection (defensive):** use market state to *reduce risk* — pause entries, de-gross the book,
  widen cushions, kill on drawdown. Robust, testable, and **missing today**. High value.
- **Prediction (offensive):** use flow/regime to *find edge / pick direction*. Disproven nine ways
  here (UW consensus, GEX, ML walk-forward, the original "intelligent bot-B" whose losers carried
  **3× the whale premium** of its winners, the information-free meta-selector at p=0.20).

**Design rule:** defensive guards ship once they reduce drawdown in backtest (accepting a small
calm-market cost). Predictive overlays ship **only** if they beat mechanical S2b out-of-sample under
the C4/C5 gate. We never let a predictive idea go live on in-sample fit. This is the guardrail that
prevents reliving the PF 5→0.5 collapse.

---

## 1. Architecture

Today: `market data → entry/exit` (per-trade, no portfolio view).

Proposed:
```
data adapters ─► Regime & Context Engine ─► RegimeState ─► Policy layer ─► bot actions
 (VIX term,        (bot/regime/*)            (one object    (bot/regime/    (size, pause,
  trend, ATR,                                 per tick)      policy.py)       de-gross, widen
  UW API, equity)                                                            cushion, kill)
```

### 1.1 `RegimeState` (the single shared signal object, recomputed each tick)
```python
@dataclass
class RegimeState:
    # raw context
    vix_level: float
    vix_pct_rank: float            # vs trailing ~120 trading days (existing logic)
    vix_term_slope: float          # VIX/VIX3M - 1 ; < 0 = backwardation = stress
    atr_pct: float                 # ATR(14)/spot
    trend_bias: str                # "up" | "down" | "neutral" (SPY vs 20/50d MA, dist from high)
    equity_drawdown: float         # fraction below the bot's own equity peak
    concentration: dict            # exposure by (expiry, strike-band)
    flow_bias: str                 # UW: "bullish" | "bearish" | "neutral"
    flow_extreme: bool             # UW: blow-out flow/skew -> risk-off veto
    # derived decisions
    stress_score: float            # 0..1 composite of the above
    risk_on: bool
    size_multiplier: float         # 0..1 applied to base_risk_pct (replaces the binary VIX halve)
    pause_entries: bool
    widen_cushion_atr: float       # additive bump to min_cushion_atr
    degross_target: float          # 0..1 fraction of book to trim now
    kill: bool                     # hard stop-everything
```

### 1.2 Data adapters (`bot/regime/`)
| Module | Source | Produces |
|---|---|---|
| `vix_term.py` | yfinance/CBOE: VIX, VIX3M, front VX | level, pct_rank, term slope |
| `trend.py` | Tradier history (already wired) | MA distances, dist-from-high → trend_bias, atr_pct |
| `flow_uw.py` | **Unusual Whales API** (REST + websocket) | flow_bias, flow_extreme, raw features |
| `concentration.py` | `BotState.open_positions` | exposure map, concentration score |
| `equity.py` | trade log / equity series | drawdown from peak |

### 1.3 Unusual Whales — concrete usage (API access confirmed)
For a SPY/index credit-spread strategy the relevant UW surfaces are **market-wide**, not single-name:
- **Market tide / net premium flow** (index-level bullish/bearish pressure)
- **Index/SPX-SPY GEX (gamma exposure)** — dealer positioning, pin/accelerant levels
- **Put/call premium & skew** — risk appetite
- **Dark pool index levels** — support/resistance context

Exact endpoints to be confirmed against the live key in Phase 0. Two distinct uses:
- **Defensive veto (Layer 2a):** `flow_extreme = True` on blow-out bearish flow + skew → risk-off
  input to the policy layer (don't sell new puts into it). Avoiding trouble, not picking winners.
- **Predictive overlay (Layer 3):** flow features as entry-edge inputs — **gated** (see §4).

### 1.4 Policy layer (`bot/regime/policy.py`)
Pure function `RegimeState → actions`. Maps the composite to:
- `size_multiplier` — continuous size dial (supersedes `sizing.regime_adjusted_risk_pct`)
- `pause_entries` — risk-off halt of *new* entries (existing trades keep being managed)
- `widen_cushion_atr` — demand more ATR distance when vol expands
- `degross_target` — proactively trim the book in stress (**the "react to a down week" capability that does not exist today**)
- `kill` — hard equity-drawdown circuit breaker

### 1.5 Integration into the existing orchestrator
- `tick()` (`orchestrator.py:136`): compute `RegimeState` once at top, thread into cycles via `Deps`.
- `run_entry_cycle` (`orchestrator.py:100`): gate on `pause_entries`; multiply size by
  `size_multiplier`; add `widen_cushion_atr` to `S2bConfig.min_cushion_atr`.
- **New** `run_degross_cycle`: when `degross_target > 0`, close the worst-cushion / most-concentrated
  positions to hit the target (reuses the existing close path + reconcile-removal).
- `kill`: sets the existing sticky halt with reason `"regime kill"`.
- **Concentration cap**: a new entry gate rejecting a trade that would exceed same-expiry /
  same-strike-band exposure limits (directly fixes the current 5×-identical-spread book).

All of this is additive — the mechanical S2b core stays intact; the engine only *gates and scales* it.

---

## 2. The dynamic reactions this unlocks (vs. today)

| Condition | Today | With the engine |
|---|---|---|
| VIX spikes mid-week | only *next* entry half-sized; open book untouched | de-gross open book, pause/scale new entries |
| SPY breaks down through MA | nothing | trend risk-off → pause new puts, widen cushion |
| VIX term backwardates | nothing | stress_score↑ → size down / pause |
| Book is 5× the same trade | nothing | concentration cap blocks the 5th correlated entry |
| Bot equity down X% from peak | nothing | kill switch halts everything |
| UW shows extreme bearish flow | nothing | defensive veto: skip new put spreads that day |

---

## 3. The honest cost
A regime guard **reduces total premium collected** and may **slightly lower win-rate in calm
markets** (it pauses/trims on some false alarms). You are buying **tail protection** — its payoff
shows up in the stress regime we haven't seen live, not in a quiet week. Net P&L can go either way
depending on stress frequency. Defensive layers are judged on **drawdown reduction**, not on beating
the unguarded book in calm periods.

---

## 4. Validation discipline (per layer)

- **Layer 1 (defensive):** backtest on history; success = lower **max drawdown / worst-week /
  higher Calmar** vs the unguarded book, with acceptable calm-market give-up. No alpha claim.
- **Layer 2a (UW veto):** pre-registered; success = vetoed days avoid losses without killing too
  many winners (measured on held-out months).
- **Layer 3 (UW predictive):** the **full C4/C5 gate** — pre-register hypotheses; H1-tune / H2-test;
  shuffled-label + permutation nulls; day-block bootstrap; **2× transaction-cost stress**; must
  **beat mechanical S2b out-of-sample**. Kill on failure. (Prior odds: low — every predecessor failed
  this. We run it rigorously and accept a likely-negative result.)

---

## 5. Phased plan (sub-specs — each independently buildable & testable)

| Phase | Deliverable | Effort | Risk |
|---|---|---|---|
| **0 — Instrument** | data adapters + shadow-log `RegimeState` next to every trade; **no behavior change** | ~1–2 d | none |
| **1 — Defensive guard** | regime engine + policy + orchestrator integration (scale/pause/de-gross/kill + concentration cap); drawdown backtest | ~3–5 d | low |
| **2 — UW veto** | `flow_uw.py` + defensive veto, pre-registered test | ~2–3 d | low |
| **3 — UW predictive** | flow-feature research + OOS harness vs S2b baseline | ~1–2 wk | high (likely negative) |

**Recommended order:** 0 → 1 → 2 → 3. Phase 0 is the keystone: it builds the labeled
regime+outcome dataset that lets us calibrate Phase 1 thresholds and run Phase 2/3 honestly, with
zero risk to the live bots. Each phase gets its own implementation plan via `writing-plans` when we
greenlight it.

---

## 6. Open decisions (resolve before Phase 1 build)
1. **Which account** does the guard protect first — the sandbox A/B, or the live cash bot ($5–15k,
   defined-risk)? (Calibrates drawdown thresholds.)
2. **Kill-switch threshold** — what equity drawdown from peak is "stop everything"? (e.g. 8–10%.)
3. **De-gross aggressiveness** — trim toward what residual exposure in stress (e.g. cut to 50%)?
4. **UW endpoints** — confirm exact market-tide / GEX / skew endpoints against the live key (Phase 0).
5. **Concentration limits** — max positions per expiry / per strike-band.

---

## 7. What this is NOT
Not a return to "predict the market with whale flow." The center of gravity is **risk control**;
prediction is a gated experiment that must earn its place. We still have exactly **one** OOS-validated
edge (S2b). Multi-strategy "pivot across 0–45 DTE" selection stays parked until the validated-edge
library has more than one member — selecting strategies on recent performance is information-free.
```
