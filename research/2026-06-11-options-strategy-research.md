# Options Strategy Evidence Review — 2026-06-11

Method: deep-research workflow — 5 parallel search angles (academic volatility premia,
exchange benchmark indexes, 0DTE/earnings research, reproducible practitioner backtests,
skeptical failure modes) → 23 sources fetched → 115 falsifiable claims extracted → top 25
adversarially verified by 3-vote panels (22 confirmed, 3 refuted). Raw output with full
citations: `2026-06-11-options-strategy-research-raw.json`.

**Mandate it serves:** find strategies with credible evidence toward an honest PF ≥ 2.0,
implementable defined-risk on a $5k–$25k account, then confirm on our 2025-03 → 2026-02
backtest data.

---

## 1. The one verified edge

**The index volatility risk premium (VRP) is real, large, persistent, and retail-accessible.**
Four independent primary sources, all unanimous on verification:
- Carr & Wu (Review of Financial Studies 2009): shorting SPX variance swaps earned log
  premia ~50%+/month (1996–2003), t-stats −7 to −9.5; information ratios above 3 —
  with the authors' own warning that such ratios flatter crash-exposed payoffs.
- VIX exceeded subsequent realized vol every year 1990–2018 except 2008 (avg 129% of RV;
  ~4.2 vol-point premium).
- The premium survives CAPM/Fama-French/momentum controls — it is structural compensation
  for crash risk, **not** a mispricing. You earn the carry only by eating the tail.
- **It lives in indexes, not single names**: variance premia insignificant in 32 of 35
  individual stocks. QQQ's premium is materially weaker than SPX's.

## 2. The honest ceiling

**No verified source documents any mechanical strategy at PF ~2.0.** The best long-run,
exchange-published records of harvesting the VRP:

| Benchmark | Mechanics | Long-run record | Worst drawdown |
|---|---|---|---|
| CBOE PUT | monthly ATM SPX put-write, cash-secured | 9.54%/yr @ 9.9% vol (Sharpe 0.64 vs SPX 0.45), 1986–2018 | −35.5% (2007–09), skew −2.1 |
| CBOE BXM | monthly ATM covered call | 12.39% vs 12.20% SPX at ⅔ vol (1988–2004) | −35.8% |
| CBOE CNDR | monthly 20Δ/5Δ iron condor, 10× max-loss collateral | ~7.2%/yr, monthly Sharpe ~0.15 | **−19%** (best survival profile) |
| CBOE BFLY | monthly ATM iron butterfly, 5% wings | risk-adj. wins only vs EAFE/commodities | −47.1% (≈ equity-like) |

Sharpe ~0.5–0.65, equity-like returns at lower vol, severe left tails. Failure regime is a
**volatility jump/crash**, not a slow decline (PUT made +6.6%/yr through the 2000–02 grind);
call-selling structures also fail in **sustained bull markets** (capped upside).

Cross-cutting caveats (carried by the verifiers, not me): benchmark "history" before
~2007–2015 launches is **backfilled and frictionless**; nearly all studies were
Cboe-commissioned (numbers reproducible, framing favorable); and the harvestable premium
**shrank materially post-2010** (Chicago Fed WP 2025-17; BXM's risk-adjusted edge flipped
negative after 2012).

## 3. 0DTE: the cost trap, precisely measured

Identified retail SPX 0DTE traders lost **>$125M aggregate (Feb 2021–Sep 2023), ~72% of it
to transaction costs**; effective spreads 5–6% of premium; long-premium (debit) orders lost
$364k/day net while credit orders were net **positive** +$122k/day. But the specific claim
that defined-risk 0DTE credit spreads earn positive median margin-adjusted returns **failed
adversarial verification (1–2)** — so short-premium 0DTE is a testable hypothesis, not a
verified edge, and any backtest must model 5–6% effective spreads, never midpoint fills.

## 4. Refuted / unanswered

- **Refuted (1–2 votes):** Bollerslev-Tauchen-Zhou VRP magnitude figures as quoted; VRP as a
  quarterly *timing* signal (R² 6.8% claim) — so the IV-RV entry filter is a hypothesis to
  test, not settled science; positive-median 0DTE credit spreads (above).
- **Unanswered (literature silent, not negative):** ML/pattern-recognition uplift over simple
  mechanical rules in options selling; post-earnings-drift option strategies; dispersion.
  Default to mechanical rules; ML remains optional future work with a *higher* validation bar.

## 5. Shortlist for our backtest (2025-03 → 2026-02, 5-min bars)

Ranked by evidence quality; all defined-risk, all sized for $5k–$25k via SPY/XSP:

1. **CNDR-analog iron condor** (SPY/XSP, short ~20Δ put+call, long ~5Δ wings, monthly and
   weekly variants; deleveraged collateral rule). Best documented survival (−19% max DD).
2. **PUT-analog defined-risk put-spread writing** (short ATM-to-OTM put + bought wing,
   monthly/weekly). Strongest long-run benchmark record.
3. **BFLY-analog ATM iron butterfly** (5% wings) — comparison arm mainly; worst drawdowns.
4. **IV-RV regime filter** overlaid on (1)/(2): sell only when implied minus trailing
   realized vol is wide. Hypothesis (timing claim was refuted) — directly testable.
5. **Short-DTE/0DTE defined-risk credit spreads, low delta, credit-only** with 5–6%
   effective-spread cost modeling. Weakest evidence, but the only variant with enough trade
   count for statistical power in one year.

Explicitly **excluded by evidence**: single-name short vol (no premium in 32/35 stocks);
long-premium single-leg 0DTE (documented retail failure mode); undefined-risk structures
(account constraint).

## 6. Statistical reality check for the PF ≥ 2.0 mandate

- Monthly strategies produce 12–24 trades/year — **no statistical power** to certify PF 2.0.
  Weekly/0DTE variants give adequate n but sit in the cost-dominated regime.
- The verified evidence tops out at Sharpe ~0.6–1.0 gross. **A one-year backtest that prints
  PF ≥ 2.0 should be presumed overfit until it survives hold-out, cost-stress, and
  regime-stress.** The window (2025-03 crash → recovery → grind) conveniently contains both a
  vol spike and a quiet bull stretch — both failure regimes are represented.
- Open question our data can directly answer: how big was the *actual* harvestable IV-RV
  premium in 2025–26? Measured first, before trusting any strategy result.

## 7. Next step

Implement 1–5 in the mechanical-entry simulator (in progress), run full-year with H1/H2
hold-out + cost stress (midpoint vs 5% effective spread), report PF/WR/DD per strategy per
half, and let the data issue the verdict.
