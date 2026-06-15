# Established, Evidence-Backed Edges in Options Trading — Sourced Research Report

Date: 2026-06-14
Scope: Identify documented, persistent options edges suitable for an **automated bot** trading a **small cash account ($5k–$15k)**, **defined-risk only** (verticals, debit/credit spreads, long options — no naked selling), **0–45 DTE**.
Honesty bar: Distinguish genuine *risk premia* (compensated) from *free lunches*. Be explicit about net-of-cost survival, edge decay, and small-account frictions. Rank by strength of evidence and net-of-cost viability.

Prior internal findings this report respects:
- **Unusual Whales flow direction does NOT predict underlying direction** (daily and intraday), disproven out-of-sample internally. → Treat all "follow the flow" claims as guilty until proven.
- **Pre-earnings IV richness / VRP is REAL but MODEST** and hard to capture net of single-name spreads (~7% half-spreads can exceed the gross edge). See `research/2026-06-13-earnings-ivcrush-research.md`.
- The internally validated baseline is **S2b** (a managed SPX/index-style put-spread setup, PF ~1.86). New candidates should be judged against it.

---

## Summary Ranking Table

| # | Edge | Source phenomenon | Real net of cost? | Evidence strength | Small-account fit | Verdict |
|---|------|-------------------|-------------------|-------------------|-------------------|---------|
| 1 | **Index variance/volatility risk premium (VRP) via defined-risk short premium on liquid index/ETF (SPX/SPY/XSP)** | Insurance demand → IV > realized vol | **Yes, modestly** — the single most-documented options risk premium; survives on penny-wide index products | **Strong** (Carr–Wu, Bollerslev, CBOE/Wilshire) | **Good** — XSP/SPY tight spreads; defined-risk caps tail | **CORE EDGE** |
| 2 | **VRP term-structure / VIX-futures contango timing of #1** | Roll yield + steep curve = elevated VRP | **Yes**, as a *conditioner* on #1, not standalone for a cash bot (VIX futures not defined-risk) | **Strong** (Quantpedia, Macrosynergy, Cheng) | Use as a signal, not a vehicle | **STRONG CONDITIONER** |
| 3 | **Weekend / calendar-day theta on index credit spreads** | 3 calendar days decay over a weekend; partial, not full, pre-pricing | **Partly** — small, largely pre-priced by Friday PM; aligns with internal S2b "Monday" finding | **Moderate** (practitioner; internal S2b corroboration) | Good — fits short DTE | **SUPPORTING / REAL BUT SMALL** |
| 4 | **0DTE index short-premium (defined-risk spreads/ratio)** | Largest per-unit VRP; intraday gamma/decay | **Marginal** — gross edge real, dies for iron-condor net; tail dominates mean | **Moderate** (Vilkov; Dim–Eraker–Vilkov) | Capital-light, PDT risk, fat tails | **SPECULATIVE / TACTICAL ONLY** |
| 5 | **Pre-earnings IV richness (single-name, defined-risk)** | Announcement VRP / IV overpriced into events | **Barely** — ~0.65% gross gap vs ~7% half-spread; net ≈ break-even | **Strong-but-negative** (Barth–So, Milian) | Poor — low freq, fat tail, wide spreads | **CONFIRMED REAL, NOT CAPTURABLE** |
| 6 | **Index vs single-name skew / dispersion / skew risk premium** | Index puts richer than basket; correlation premium | Gross only — needs many legs/short vega; not defined-risk-friendly at $5–15k | **Strong academically** (Wallmeier; Kozhan) | Poor — leg count, capital | **REAL BUT NOT FEASIBLE HERE** |
| 7 | **Overnight / weekend drift in the underlying** | Overnight equity drift anomaly | **No** (for a bot) — microstructure-inflated; live products (NightShares) failed and closed | **Weak/decayed** | n/a | **REJECT** |
| 8 | **Options order-flow / put-call ratio directional prediction** | "Smart money" informed flow | **No** — internally disproven (UW); literature modest & decayed | **Weak** | n/a | **REJECT (per internal OOS)** |

---

## 1. Index Variance / Volatility Risk Premium (VRP) — the core, best-supported edge

**The edge and its source.** Across equity-index options, implied volatility systematically exceeds subsequently realized volatility. The gap (the VRP) is the compensation option *sellers* earn for bearing the risk that volatility spikes — i.e., for selling insurance to a market structurally long protection demand. This is the most heavily documented risk premium in options.

- Carr & Wu, *Variance Risk Premia* (Review of Financial Studies, 2009): the index variance risk premium is large, negative for buyers, and time-varying — sellers are compensated. [PDF](https://engineering.nyu.edu/sites/default/files/2019-01/CarrReviewofFinStudiesMarch2009-a.pdf)
- Coval & Shumway, *Expected Option Returns*: zero-beta straddles lose ~3%/week — direct evidence option *buyers* overpay, sellers are paid. (via [Quantpedia VRP](https://quantpedia.com/strategies/volatility-risk-premium-effect/))
- CBOE/Wilshire benchmark studies (PUT, BXM, BXMD): index premium-selling indexes delivered higher risk-adjusted returns than the S&P 500 over 30 years; PUT Sharpe ~46% above SPX. [Wilshire 2019 PDF](https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf) · [CBOE white paper](https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/)

**Real and capturable net of costs?** **Yes — this is the one to build around — but only on *liquid index products*.** The reason single-name VRP dies (per internal earnings research: ~7% half-spreads > gross edge) is *transaction cost*, not absence of edge. Index/ETF options (SPX, **XSP** = mini-SPX, SPY) trade penny-to-nickel wide, so the same risk premium survives. Critical caveats:
- It is a **risk premium, not a free lunch**: it pays you for bearing correlated tail risk and loses precisely in crashes (Coval–Shumway straddle "−800%" days; PutWrite max drawdown ~−24% historically). **Defined-risk structures (put credit spreads, iron condors, put butterflies) are the correct expression for a $5–15k account** because they cap that tail at a known dollar amount — the single most important adaptation versus the naked-selling indices the academic literature backtests.
- CBOE index returns are **gross of costs and use idealized fills**; real implementations underperform. The skeptical literature (AQR *PutWrite vs BuyWrite*; CBOE *BXM/PUT Conundrum*) shows the *structure* (puts vs calls) is put-call-parity-equivalent and the realized edge is real but smaller than headline. [AQR PDF](https://www.aqr.com/-/media/AQR/Documents/Insights/White-Papers/AQR-PutWrite-vs-BuyWritevF.pdf) · [CBOE Conundrum PDF](https://cdn.cboe.com/resources/indices/documents/bxm-put-conundrum.pdf)

**Factors / conditions that signal the edge is present (measurable triggers):**
- **VRP gap**: 30-day implied vol (VIX) minus a realized-vol forecast (e.g., trailing 21d RV or HAR/EWMA). Positive and wide = richer premium.
- **IV Rank / IV Percentile** of the index (tastytrade-style 0–100 over trailing 1y). Higher IV rank historically = more premium and better short-vol expectancy. [tastytrade volatility metrics](https://support.tastytrade.com/support/s/solutions/articles/43000539059)
- **Term-structure slope** (see #2): contango = sell into elevated premium.
- **Realized-vs-implied gap regime**: if realized has been *exceeding* implied recently (backwardation/stress), stand down.

**Real-time identification & structure.** Daily (or a few intraday checkpoints). Compute VIX, an RV forecast, IV rank, and term slope at the open/midday. When VRP positive + IV rank elevated + curve in contango → **sell a defined-risk put credit spread or iron condor on XSP/SPY**, ~30–45 DTE, short leg ~15–25 delta, manage at ~50% max profit or ~21 DTE (tastytrade-style management). 0–45 DTE constraint is satisfied; 30–45 DTE is the sweet spot for premium-to-gamma balance.

**Small-account fit.** **Excellent.** XSP is 1/10 SPX notional, cash-settled, European-style (no assignment), Section 1256 tax treatment, and defined-risk spreads have a *known max loss* = perfect for $5–15k. Frequency is flexible (weekly/monthly entries). Main friction: PDT rule and settled-funds (cash account, T+1) limit how often you can recycle capital — favors fewer, longer-dated positions over rapid 0DTE churn.

---

## 2. VRP Term Structure / VIX-Futures Contango — the best *conditioner*

**Edge/source.** The VIX-futures curve is usually in **contango** (front < back), reflecting a roll-yield/term VRP. The steepness of implied-vol term structure predicts when short-vol carry is richest. [Quantpedia: Exploiting Term Structure of VIX Futures](https://quantpedia.com/strategies/exploiting-term-structure-of-vix-futures/) · [Macrosynergy: VIX term structure as a trading signal](https://macrosynergy.com/research/vix-term-structure-as-a-trading-signal/) · [QuantSeeker: Timing Volatility with the VIX Term Structure](https://www.quantseeker.com/p/timing-volatility-with-the-vix-term)

**Net of cost?** As a **standalone vehicle it is *not* defined-risk** (short VIX futures / SVOL-style products have unbounded tail; see Volmageddon, XIV −96%). But as a **regime filter for edge #1 it is high-value and free to compute.** Contango → short premium favored; backwardation → suppress or flip to long-vega/defensive.

**Triggers:** VIX/VIX3M ratio < 1 (contango) is the canonical "risk-on for short premium" signal; > 1 (backwardation) = stand down. Front/second VIX-future basis sign. Roll yield.

**Real-time/structure.** Daily computation of VIX term ratio gates whether the #1 short-premium spread is allowed to fire. **Use it as a switch, not a trade.**

**Small-account fit.** Perfect as a *signal* (no capital needed); do **not** trade VIX futures directly in a defined-risk cash account.

---

## 3. Weekend / Calendar-Day Theta on Index Credit Spreads

**Edge/source.** Theta is calendar-based: a short option held over a weekend decays ~3 calendar days while only ~1 trading day of underlying risk is borne in price terms (markets closed). Short premium therefore *appears* to harvest extra decay Fri→Mon. [Moontower: Weekend Theta](https://blog.moontower.ai/weekend-theta/) · [SpotGamma](https://support.spotgamma.com/hc/en-us/articles/15249846531219-Weekend-Theta)

**Net of cost / is it real?** **Partly, and small.** Market makers *anti-arbitrage* this — IV is bled down into Friday's close so the "free" weekend decay is largely **pre-priced by Friday afternoon**. What remains is a modest, real effect plus weekend **gap risk** (the buyer is paying for the long-gamma optionality of a closed-market gap). **This corroborates the internal S2b "Monday-only" finding**: entering short-premium positions to *capture* (not pay for) the weekend is consistent with where the residual edge sits. Defined-risk spreads cap the gap tail.

**Triggers:** Day-of-week (enter Friday PM / Monday per internal S2b evidence), DTE spanning a weekend or holiday (3-day decay), normal/elevated IV rank, contango.

**Real-time/structure.** Daily; deterministic calendar trigger. Express via the same XSP/SPY defined-risk put credit spread as #1 — this is a *timing overlay* on the core VRP edge, not an independent edge.

**Small-account fit.** Good — naturally short-DTE, low capital, defined-risk. Honest note: alone it is too small to be a primary edge; its value is as a **timing rule layered on #1**, and it is already partially reflected in the validated S2b.

---

## 4. 0DTE Index Short-Premium (defined-risk spreads / ratios)

**Edge/source.** 0DTE index options carry the **largest per-unit variance risk premium** (max risk concentration → max compensation) and are driven intraday by decay and dealer gamma. [Vilkov, *0DTE Trading Rules* (SSRN 4641356)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4641356) · [Dim–Eraker–Vilkov, *0DTEs: Trading, Gamma Risk and Volatility Propagation*](https://papers.ssrn.com/sol3/Delivery.cfm/4692190.pdf?abstractid=4692190)

**Net of cost?** **Marginal and structure-dependent — the most honest finding in the recent literature.** Vilkov's strict out-of-sample (Apr-2019→Feb-2026, 252-day rolling) results, *after costs*:
- Put **ratio spreads**: gross SR 1.18 → **net 0.93** (survives).
- Strangle/straddle: 0.56 → **0.39**.
- **Iron butterfly/condor: 0.77 gross → −0.20 net** (the defined-risk version *dies* after half-spreads + fees).
- Equal-weight basket of top-3: 1.12 → **0.82**.

Key verdict quotes: median realized 0DTE VRP was only ~**0.0011% of underlying** ("difficult to monetize after realistic frictions"); **expected shortfall (1%) 0.58–1.58% of underlying dwarfs mean PNL**; "unconditional 0DTE exposure is difficult to justify as a standing allocation." The edge, where it exists, requires **directional/classification filters** (logistic "P(positive PNL)") and **time-of-day entries (10:00 ET)** — not unconditional selling.

**Triggers:** Time-of-day (10:00 ET tested best), intraday IV/VRP estimate, dealer-gamma regime (positive GEX = mean-reverting/range = friendlier to short premium; negative GEX = trending = hostile), realized-vol-of-day. Note GEX itself is **practitioner-grade, not peer-reviewed**, and "high gamma ≠ deterministic pinning." [SpotGamma GEX](https://spotgamma.com/gamma-exposure-gex/)

**Real-time/structure.** Pure intraday. The *defined-risk* expression a cash account can use (iron condor/butterfly) is exactly the structure that went **net-negative** in Vilkov. The survivor (put ratio spread) has an uncovered tail unless capped, pushing it toward a broken-wing structure with worse economics.

**Small-account fit.** Capital-light and 0-DTE by definition, BUT: (a) **PDT rule** bites hardest here (intraday round-trips), (b) **tail risk dominates** so sizing must be tiny, (c) the defined-risk version is the one that fails net. **Speculative / tactical overlay at most — not a core allocation.** Strictly inferior to #1 on net edge and tail.

---

## 5. Pre-Earnings IV Richness (single-name, defined-risk)

**Already deeply researched internally — included for completeness and ranking.** Real but **not capturable net of single-name spreads.** Barth & So (2014): excess implied vol +0.149, t=13.5 — IV genuinely overpriced into earnings, but it is a *compensated announcement risk premium*. Milian (2023): implied move ~5.72% vs realized ~5.07% → only ~**0.65% gross gap** whose IQR straddles zero, against a **~7.4% one-day straddle half-spread**. Signals decayed to insignificance 2014–2017; ORATS' 2026 season *inverted*. Sharply negative skew (−26.7% single-trade tail). [Cross-sectional IV predictability (UCLA)](https://www.anderson.ucla.edu/documents/areas/fac/finance/cross_options.pdf) · internal `2026-06-13-earnings-ivcrush-research.md`.

**Verdict: confirmed-real, not-capturable.** Defined-risk structures (iron fly/condor) trade fewer legs and *might* improve the cost profile — the one open empirical crack — but the prior is break-even and it does not plausibly clear S2b. Low frequency + fat tail + wide spreads = **poor small-account fit.**

---

## 6. Index-vs-Single-Name Skew / Dispersion / Skew Risk Premium

**Edge/source.** Index options embed a richer (more negative) skew and higher implied correlation than the basket of constituents — a **correlation/skew risk premium**. Strong academic support: Wallmeier (*Skewness Premium and Index Option Returns*, 2016/17); CFA Institute *Skew Risk Premium in the Equity Index Market*; skew and variance premia are intertwined (hedging one diminishes the other). [Wallmeier PDF](https://efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2017-Athens/papers/EFMA2017_0059_fullpaper.pdf) · [CFA digest](https://rpc.cfainstitute.org/research/cfa-digest/2013/11/the-skew-risk-premium-in-the-equity-index-market-digest-summary)

**Net of cost / feasibility.** The premium is real but harvesting **dispersion** requires being **short index vol vs long many single-name vols** — dozens of legs, meaningful short-vega/correlation exposure, and capital well beyond $5–15k. The skew premium only reliably pays "when risk aversion is high" (regime-dependent). **Not implementable as defined-risk in a small account.**

**Small-account fit. Poor — reject for this project** despite strong academic support. (Mentioned because it's a major documented edge; the constraint, not the evidence, kills it.)

---

## 7. Overnight / Weekend Drift in the Underlying — REJECT

**Edge/source.** The "overnight effect": equities historically earn most of their return close-to-open, little/negative open-to-close. Called the "grandmother of all market anomalies." [Elm Wealth](https://elmwealth.com/night-moves-overnight-drift/) · [STOXX](https://stoxx.com/when-do-returns-come-from-an-analysis-of-the-overnight-effect-in-equities-trading/)

**Net of cost?** **No.** Research shows much of the ETF overnight return is **microstructure-inflated** (overnight order imbalances + widened spreads add ~6%/yr artificially; ScienceDirect). Decisively: **NightShares launched live overnight-drift ETFs in 2022 and closed both in 2023 after losses/underperformance** — a real-world out-of-sample failure. Consistent with the internal UW-flow disproof: a daily-direction prediction edge that dies live. **Reject.**

---

## 8. Options Order-Flow / Put-Call Ratio Directional Prediction — REJECT

**Edge/source.** Claims that informed options flow (or PCR) predicts underlying direction. Literature is mixed-to-modest: open-interest PCR has *some* weekly predictive power; "stocks with expensive calls beat expensive puts by ~50 bps/week" — but explicitly "less reliable than financial media suggests" and decays under costs. [MDPI PCR study](https://www.mdpi.com/2227-7099/7/1/24)

**Net of cost?** **No, for this project.** Internal research already **disproved Unusual Whales flow direction out-of-sample, daily and intraday.** The weak academic cross-sectional signal does not survive single-name spreads and contradicts the internal OOS result. **Reject as a directional edge.** (PCR/flow may retain marginal value only as a *contrarian sentiment regime gauge*, never as a directional trigger.)

---

## Synthesis & Recommendation

1. **Build the bot around Edge #1 (index VRP via defined-risk short premium on XSP/SPY/SPX-mini).** It is the best-documented options risk premium, it *survives net of cost on liquid index products* (the reason single-name VRP fails is spreads, not absence of edge), and defined-risk spreads cap the crash tail that sinks naked premium-sellers. This is also what the internally validated S2b already exploits — so the evidence and the internal OOS agree.
2. **Gate entries with Edge #2 (VIX term-structure contango) and IV-rank**, and **time them with Edge #3 (weekend/Monday calendar)** — both are free-to-compute conditioners that sharpen #1 rather than independent strategies. The S2b "Monday" result is the empirical fingerprint of this.
3. **Treat Edge #4 (0DTE) as a speculative tactical overlay only.** The honest OOS literature shows the *defined-risk* 0DTE structures go net-negative after costs; the survivors carry uncapped tails. Worse than #1 on net edge, tail, and PDT friction.
4. **Do not chase #5–#8.** #5 (earnings) is real-but-not-capturable and #6 (dispersion/skew) is real-but-infeasible at this account size; #7 (overnight drift) and #8 (flow direction) are rejected on live-failure / internal OOS-disproof grounds.

**Strongest single takeaway:** the durable, capturable, small-account-appropriate options edge is the **index variance risk premium harvested with defined-risk spreads on penny-wide index products, conditioned on VRP/IV-rank/term-structure regime** — a compensated risk premium, not a free lunch, expressed in a structure that caps the tail. Everything else either dies to single-name costs, requires capital/leg-count a $5–15k account doesn't have, or has already failed internally/live.

---

## Sources
- Carr & Wu, *Variance Risk Premia*, RFS 2009 — https://engineering.nyu.edu/sites/default/files/2019-01/CarrReviewofFinStudiesMarch2009-a.pdf
- Quantpedia, *Volatility Risk Premium Effect* — https://quantpedia.com/strategies/volatility-risk-premium-effect/
- Quantpedia, *Exploiting Term Structure of VIX Futures* — https://quantpedia.com/strategies/exploiting-term-structure-of-vix-futures/
- CBOE/Wilshire, *Options-Based Benchmark Indexes* (2019) — https://cdn.cboe.com/resources/spx/wilshire-options-based-benchmark-indexes-2019.pdf
- CBOE white paper, VRP and PUT index — https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/
- AQR, *PutWrite versus BuyWrite* — https://www.aqr.com/-/media/AQR/Documents/Insights/White-Papers/AQR-PutWrite-vs-BuyWritevF.pdf
- CBOE, *The BXM and PUT Conundrum* (Shalen) — https://cdn.cboe.com/resources/indices/documents/bxm-put-conundrum.pdf
- Macrosynergy, *VIX term structure as a trading signal* — https://macrosynergy.com/research/vix-term-structure-as-a-trading-signal/
- QuantSeeker, *Timing Volatility with the VIX Term Structure* — https://www.quantseeker.com/p/timing-volatility-with-the-vix-term
- Vilkov, *0DTE Trading Rules* (SSRN 4641356) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4641356 ; annotated repo — https://github.com/vilkovgr/0dte-strategies/blob/main/docs/paper/paper-annotated.md
- Dim, Eraker & Vilkov, *0DTEs: Trading, Gamma Risk and Volatility Propagation* — https://papers.ssrn.com/sol3/Delivery.cfm/4692190.pdf?abstractid=4692190
- Wallmeier, *Skewness Premium and Index Option Returns* — https://efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2017-Athens/papers/EFMA2017_0059_fullpaper.pdf
- CFA Institute, *The Skew Risk Premium in the Equity Index Market* — https://rpc.cfainstitute.org/research/cfa-digest/2013/11/the-skew-risk-premium-in-the-equity-index-market-digest-summary
- Elm Wealth, *Night Moves: Overnight Drift* — https://elmwealth.com/night-moves-overnight-drift/
- STOXX, *When do returns come from?* — https://stoxx.com/when-do-returns-come-from-an-analysis-of-the-overnight-effect-in-equities-trading/
- MDPI Economies, *Put–Call Ratio Volume vs Open Interest in Predicting Market Return* — https://www.mdpi.com/2227-7099/7/1/24
- UCLA Anderson, *Option Returns and Cross-Sectional Predictability of Implied Volatility* — https://www.anderson.ucla.edu/documents/areas/fac/finance/cross_options.pdf
- Moontower, *Weekend Theta* — https://blog.moontower.ai/weekend-theta/
- SpotGamma, *Gamma Exposure (GEX)* — https://spotgamma.com/gamma-exposure-gex/
- tastytrade, *Volatility Metrics (IV Rank/Percentile)* — https://support.tastytrade.com/support/s/solutions/articles/43000539059
- Internal: `research/2026-06-13-earnings-ivcrush-research.md` (Barth–So 2014; Milian 2023; ORATS); S2b validation commits.
