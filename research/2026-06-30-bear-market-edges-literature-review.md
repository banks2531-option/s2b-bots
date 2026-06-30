# Bear-Market / Sell-Off Edges — Exhaustive Literature Review

Date: 2026-06-30 · Method: 5 parallel research agents across academic (SSRN/journals), practitioner
(AQR/CBOE/CTA), volatility/tail, GitHub/open-source, and forums (Reddit/Quantpedia/blogs). Goal: find
a VALIDATED systematic edge for adverse conditions, assessed for retail-testability on our stack
(free daily index/ETF/VIX history + Databento OPRA 2013+ + VX futures).

## The unanimous #1 answer: trend-following ("crisis alpha") — but MULTI-ASSET
All five streams independently converged: systematic **time-series-momentum / managed-futures trend**
is the one robustly-evidenced edge in crises. Moskowitz-Ooi-Pedersen (2012, JFE, Sharpe ~1.3, "highest
profits in the most extreme episodes"); Hurst-Ooi-Pedersen (2017, JPM, "Century of Evidence" — positive
every decade 1880–2016, positive in **8 of 10 largest 60/40 drawdowns**). CTAs: **2008 ~+13%, 2022
+20–27%** (SG CTA/Trend indices).

**Critical mechanism + our-2022-puzzle resolved:** crisis alpha tracks crisis DURATION, not existence
(Kaminski/AlphaSimplex: tech-bust +62%, GFC +32%, 2022 +36%, but **COVID −2%, Q4-2018 −3%** — fast
V-crashes whipsaw it). And **2022's profit was SHORT BONDS (largest leg) + LONG commodities + LONG USD;
EQUITIES were a net DETRACTOR.** So equity-only S&P trend (our `trend_test.py`, which whipsawed in 2022)
is structurally insufficient — **the real edge is MULTI-ASSET with a short side.**

## What the evidence KILLS (documented losers — do not (re)deploy)
- **Buying puts / tail hedge:** AQR "Put vs Trend" (2020): 5%-OTM puts **−6.4%/yr geo, Sharpe −0.61,
  −92% DD**, negative every decade. Israelov "Pathetic Protection" (JAI 2019): protective puts lose to
  simply holding less equity 97–100% of the time (PPUT alpha −1.8%/yr). Universa/Spitznagel is real but
  a *manager* edge (deep-OTM sourcing/scale), unverifiable & not retail-replicable; ~half its edge is
  in-sample (one crash, n=1).
- **Long vol / short VXX:** structural roll bleed (VXX ~−98% since 2009); naive short-vol detonates —
  **XIV −96% in a day**, Volmageddon Feb 5 2018 (Augustin-Cheng FAJ 2021). Term-structure filters
  (Simon-Campasano 2014) thin the tail but **deteriorate OOS** and lag the gap.
- **Put-selling as "protection":** CBOE PUT index **beta ~0.56** — it FALLS in crashes (just less),
  caps upside. It's lower-beta long / VRP income, **NOT a hedge** (the thetagang-2022 reckoning).
- **VRP itself may be DECAYING:** Dew-Becker & Giglio (2025, Chicago Fed WP 2025-17) "The Decline of the
  Variance Risk Premium" — index-option alphas "indistinguishable from zero" post-~2018 (crowding).
- **Un-gated mean-reversion (RSI-2):** fails in bears (win-rate <60% in 2008/2020 — oversold gets more
  oversold). Only a SIGNAL when trend-gated (200-DMA + VIX ceiling) → becomes a bull-pullback play.
- **Dispersion / correlation premium:** real but institutional/OTC, decayed post-2000, and corr→1 in a
  crash so it's not even a hedge. Reject for retail.
- **Cross-sectional momentum:** CRASHES in rebounds (Daniel-Moskowitz 2016: loser decile +163% in 2009).
  Portable lesson: **vol-target / de-gross in panic** — improves any directional signal.

## The two engines (never conflate)
- **Make money DURING the crash** = trend / long-vol / tail = CONVEX, costs carry in calm times.
- **Make money AFTER the panic** = mean-reversion / sell-vol = CONCAVE, blows up if early.

## Testable candidates for our stack (ranked)
1. **★ Multi-asset trend with a short side** (SPY, TLT/short-bonds, DBC or GSG, GLD, UUP), 200d/12mo
   signal, vol-scaled. FREE daily-ETF backtest. The decisive test: **isolate the 2022 delta** vs
   equity-only — does adding bonds/commodities/USD + shorts capture what S&P-only missed?
2. **VRP-decline test on our OWN OPRA data** — split 2013–2017 vs 2018–2025; is the premium S2b
   harvests still there post-2018? (Dew-Becker.) Directly relevant to the live bot's premise.
3. **Trend-gated mean-reversion** (RSI-2 long only when SPY>200d & VIX<~25) — modest, decayed.
4. **Regime-conditioned defined-risk short put-spread** (sell only in contango + elevated IV−RV);
   skeptic prior: the DEFINED max-loss, not the signal, is what saves you; our own IVR conditioning
   failed 3× before — re-test, don't assume.

## Open-source tooling worth adapting
- **lambdaclass/options_portfolio_backtester** — real SPY option chains 2008–2025, honest swappable
  fills, BYO-CSV schema → adapt our Databento OPRA, honestly backtest put-selling/spreads through
  2008 + Mar-2020 (the crash tearsheet no public put-SELLING repo shows).
- **smangj/vix_option** (PLOS One 2024) — genuinely walk-forward VIX strategy whose TEST window
  contains Feb-2018/Mar-2020/2022; needs VX futures (Databento).
- **Faber GTAA / handiko RSI-2** — free-SPY baselines.

## Verdict
Your premise holds: a validated edge for adverse conditions exists — **multi-asset trend-following
(crisis alpha)** — and it's the single thing the entire literature agrees on. It is DEFENSIVE/convex
(rides sustained trends, whipsaws in fast V-crashes), and its real power needs **breadth + a short
side**, which our equity-only test lacked. Every *offensive options* play to profit from a crash is
either negative-carry insurance, a blow-up, or institutional-only. The actionable next step is to test
**multi-asset trend with shorts** and isolate the 2022 delta — free, and it directly extends Phase 1.
