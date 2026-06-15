# Options Trade Factor Reference

**Purpose.** A complete catalogue of the factors that can be reviewed *before executing an options trade* to judge its quality. Each factor has: what it is, why it matters for the execute/skip decision, how to measure it, a **quality rubric** (Favorable / Neutral / Adverse), its **directionality** (whether "good" flips depending on whether you're a net premium *seller* or *buyer*), and a **data tag** for how reliably we can reconstruct it in *this* project.

This is the scoring schema used to dissect the live bot-B trades (`research/2026-06-14-livetrade-factor-analysis*`). It is a living document — add/refine factors as evidence accumulates.

### How to read the rubric
- Each factor is scored **+1 (Favorable) / 0 (Neutral) / −1 (Adverse)** *relative to the trade's own thesis*.
- **Directionality** matters: e.g. high IV rank is Favorable for a credit spread (you sell rich premium) but Adverse for a debit spread (you buy rich premium). Always score relative to the structure being placed.
- **Data tag** (this project): **[D]** Direct from trade/price data · **[M]** Modeled/approximated (BS, proxies) · **[P]** Partial coverage (e.g. ticker-date flow join, post-2026-03-04 only) · **[X]** Not available locally (droplet/live-feed only).

---

## Category A — Market & Regime Context
*(Is the overall environment hospitable to this structure right now?)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| A1 | **Index trend (SPY)** | Credit-spread directionality lives or dies with the tape; a bull put spread in a downtrend is fighting the market. | SPY vs 20/50-day SMA stack; slope | Aligned w/ trade bias / flat / against | both | [D] |
| A2 | **Volatility regime (VIX level)** | High VIX = richer premium but bigger moves; very low VIX = thin premium, complacency. | VIX absolute + percentile | Premium-sell: mid-high VIX; debit: low-rising | flips | [D] |
| A3 | **VIX term structure** | Contango (normal) vs backwardation (stress) signals whether short-vol is being paid or punished. | VIX vs VIX3M / front-vs-back future | Contango for short-vol / backwardation adverse | flips | [M] |
| A4 | **Sector rotation** | Legacy finding: bear-call in cyclical-led tape PF 0.05 vs 1.36 otherwise. Rotation gates directional credit. | Cyclicals (XLY/K/I/F) vs defensives (XLP/U/V) 20d RS | Trade bias matches leadership | both | [M] |
| A5 | **Market breadth** | Thin breadth under a rising index = fragile; broad breadth = durable trend. | % above 50d MA, adv/decl, McClellan | Broad supports trend trades | both | [P] |
| A6 | **Macro event proximity** | FOMC/CPI/NFP days have outsized gap risk and IV crush; many edges invert around them. | Days to next scheduled macro print | No imminent event / 1–2 days out adverse | both | [M] |
| A7 | **Time of day** | Open is noisy/wide; midday liquidity thins; last hour trends. Fills + gamma differ. | ET clock at entry | 10:00–11:00 & 14:00–15:30 best | both | [D] |
| A8 | **Day of week / seasonality** | Weekend theta (validated S2b edge), Monday/Friday effects, OPEX weeks. | Weekday, OPEX proximity, month | Strategy-specific (S2b: Mon) | both | [D] |

## Category B — Underlying Technicals
*(Is the specific name positioned well for the trade's directional/neutral thesis?)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| B1 | **Price vs moving averages** | Trend context for the short strike; selling puts under support is dangerous. | Close vs 20/50/200 SMA; dist in ATRs | Short strike on the safe side of trend | both | [D] |
| B2 | **RSI(14) / momentum** | Overbought/oversold conditions the odds of continuation vs snap-back. | RSI, 5/10/20d returns | Not stretched against the trade | both | [D] |
| B3 | **Realized volatility** | Drives how far the underlying can travel toward the short strike. | Close-to-close RV 5/10/21d (annualized) | Low/stable RV for premium-sell | flips | [D] |
| B4 | **ATR / expected daily range** | Sizing the buffer between spot and short strike. | ATR(14) as % of price | Short strike ≥ N×ATR away | premium-sell | [D] |
| B5 | **Distance to recent hi/lo** | Proximity to breakout/breakdown raises tail odds. | % from trailing-20d high/low | Cushion present | both | [D] |
| B6 | **Entry-day gap / intraday drift** | Gaps change the picture the bot was reacting to. | Open vs prior close; spot vs open at entry | No adverse gap into the strike | both | [D] |
| B7 | **Support/resistance & round numbers** | Short strikes parked just beyond real S/R have higher hold odds. | Strike vs visible S/R levels | Strike protected by a level | premium-sell | [M] |

## Category C — Volatility & Pricing
*(Are you being paid enough for the risk — the single biggest edge lever?)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| C1 | **Implied volatility level** | Sets the premium you collect/pay. | ATM IV of the expiry | High for sellers / low for buyers | flips | [P] |
| C2 | **IV Rank / IV Percentile** | The *single most cited* premium-selling filter: are you selling rich or cheap vol relative to the name's own history? | IV vs trailing 1-yr range | IVR high (>50) sell / low buy | flips | [X]→[M] |
| C3 | **Implied vs Realized (VRP)** | The actual edge in premium selling — IV should exceed subsequent RV. | IV − RV (same horizon) | IV ≫ RV for sellers | flips | [M] |
| C4 | **Volatility skew** | Put skew makes put spreads richer; informs which side to sell. | 25Δ put IV − 25Δ call IV | Selling the rich wing | premium-sell | [X] |
| C5 | **IV term structure (single name)** | Front rich vs back informs calendar vs vertical and DTE choice. | Front-expiry IV vs further | Front rich for short-dated sell | flips | [X] |
| C6 | **Earnings/event IV inflation** | Pre-event IV is structurally rich then crushes — both a trap (long) and an edge (short, with tail risk). | IV vs non-event baseline; days-to-earnings | Defined-risk short into crush | flips | [M] |

## Category D — Event & Catalyst
| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| D1 | **Days to earnings** | Holding a short spread THROUGH earnings = uncapped-direction gap risk; the #1 avoidable blowup. | Calendar to next confirmed earnings | Expiry before earnings (sellers) | both | [M] |
| D2 | **Earnings just occurred** | Post-earnings IV is crushed and drift (PEAD) may persist. | Days since last earnings | Post-crush for premium-sell | flips | [M] |
| D3 | **Ex-dividend / assignment risk** | Early assignment on ITM short calls around ex-div. | Ex-div date vs short-call moneyness | No ITM short call over ex-div | premium-sell | [M] |
| D4 | **Known catalysts** | FDA, product launches, guidance — discrete tail events. | News/catalyst calendar | None imminent | both | [X] |

## Category E — Contract Greeks & Strike Selection
| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| E1 | **Short-strike delta** | Best single proxy for probability of the short strike going ITM (≈ prob of loss). | Δ of short leg | ~0.15–0.30 for credit spreads | premium-sell | [M] |
| E2 | **DTE (days to expiry)** | Governs theta decay rate, gamma risk, and how long the thesis must hold. | Expiry − entry | Strategy-fit (e.g. 0–7 weekly sell, 30–45 classic) | both | [D] |
| E3 | **Gamma exposure** | Short-dated short options have explosive gamma near the strike → fast losses. | Γ of short leg, esp ≤2 DTE | Low gamma unless intended | premium-sell | [M] |
| E4 | **Theta / day** | The premium-seller's income; the buyer's bleed. | Θ of position | High Θ for sellers | flips | [M] |
| E5 | **Vega exposure** | Sensitivity to IV change — matters most around events/regime shifts. | Net Vega | Low/negative for sellers in high IV | flips | [M] |
| E6 | **Moneyness / OTM cushion** | How far OTM the short strike sits = the safety buffer. | (Spot − short strike)/Spot | Comfortable OTM cushion | premium-sell | [D] |
| E7 | **Probability ITM / POP** | Model probability the trade finishes a winner. | From Δ or BS; POP for the structure | High POP **with** acceptable payoff | both | [M] |

## Category F — Trade Structure & Economics
*(Is the risk/reward itself any good, independent of timing?)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| F1 | **Strategy type** | Credit vs debit, vertical vs calendar — must match the vol/trend thesis. | Label | Matches regime (C/A) | both | [D] |
| F2 | **Credit-to-width (return on risk)** | For a credit spread, credit/width sets max ROR and breakeven; too-thin credit = bad payoff. | credit ÷ wing width | ~0.25–0.40 of width | premium-sell | [D] |
| F3 | **Max profit / max loss / RR** | Defines the payoff asymmetry — the bot-B failure was losers ≫ winners. | From strikes + credit | Loss capped, RR sane | both | [D] |
| F4 | **Breakeven distance** | How much room before the trade loses money. | BE vs spot, in % and ATRs | BE beyond a real buffer | both | [D] |
| F5 | **Expected value (modeled)** | POP × avg-win − (1−POP) × avg-loss, net of costs. | Combine E7 + F3 + costs | Positive EV after costs | both | [M] |
| F6 | **Wing width vs account** | Width sets dollar risk; must fit cash-account sizing & settled funds. | width×100×qty vs equity | ≤ per-trade risk budget | both | [D] |

## Category G — Liquidity & Microstructure
*(Can you actually get in and out without the spread eating the edge?)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| G1 | **Option bid/ask spread** | The dominant hidden cost; single-name spreads can exceed the entire gross edge. | (ask−bid)/mid per leg | Tight (≤5–10% of credit) | both | [X]→[M] |
| G2 | **Open interest / volume** | Thin OI = bad fills, hard exits, pin risk. | OI & day volume at strike | Deep, liquid chain | both | [X] |
| G3 | **Underlying liquidity** | Mega-cap/ETF = tight; small-cap = slippage. | ADV, market cap | Liquid core only (small acct) | both | [D] |
| G4 | **Slippage estimate** | Realistic fill vs mid; backtests die here. | Modeled half-spread | Low modeled slip | both | [M] |

## Category H — Flow & Positioning
*(What are informed participants / dealers doing? — handle with documented skepticism.)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| H1 | **Unusual options activity** | Large/aggressive sweeps *can* mark positioning — but **this project proved flow direction does NOT predict underlying direction OOS (daily & intraday).** Use as context, not a trigger. | UW sweep/premium/side | Context only; not a standalone edge | weak | [P] |
| H2 | **Dark-pool / floor prints** | Large off-exchange prints — **measured OOS as a mild *fade*, not a follow.** | UW `has_floor`, direction | Treat with skepticism | weak | [P] |
| H3 | **Put/Call ratio & sentiment** | Extremes can be contrarian. | Index/name P/C ratio | Extremes for mean-reversion | both | [P] |
| H4 | **Dealer gamma / GEX** | Positive GEX pins/dampens; negative GEX accelerates moves — a *structural* (not predictive-flow) effect with better evidence than H1/H2. | Estimated dealer gamma | Positive GEX for premium-sell | premium-sell | [X] |

## Category I — Portfolio & Risk Management
*(Even a great trade can be wrong given what you already hold.)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| I1 | **Position size vs equity** | Over-sizing turns a normal loss into ruin. | risk$ ÷ equity | ≤ 1–2% (small acct) per trade | both | [D] |
| I2 | **Correlation to open book** | Five "different" trades that are all short-SPY-beta is one trade. | Beta/sector overlap w/ open positions | Low correlation | both | [D] |
| I3 | **Total open risk / concentration** | Aggregate tail across the book. | Σ risk vs equity; per-ticker cap | Within caps; ≤3 concurrent | both | [D] |
| I4 | **Same-ticker cooldown** | Legacy COIN lesson: re-entering a name right after a loss. | Sessions since last loss in name | Past cooldown window | both | [D] |
| I5 | **Settled-funds availability** | Cash account: unsettled proceeds can't fund new trades (good-faith violations). | Settled cash vs trade cost | Funded by settled cash | both | [D] |

## Category J — Timing & Management Plan
*(A trade is a plan, not just an entry.)*

| # | Factor | Why it matters | Measure | Favorable / Neutral / Adverse | Dir. | Data |
|---|---|---|---|---|---|---|
| J1 | **Entry timing quality** | Chasing vs waiting for a level/IV pop changes expectancy. | Entry vs intraday context | Disciplined, not chasing | both | [D] |
| J2 | **Profit-target rule** | Taking 50% of max credit is a documented manage-winners edge. | TP as % of credit | Defined (e.g. 50%) | premium-sell | [D] |
| J3 | **Stop / max-loss rule** | The bot-B problem: no effective loss cap → losers ≫ winners. | Stop as ×credit or Δ-based | Defined and enforced | both | [D] |
| J4 | **Time-based / DTE exit** | Avoid gamma + assignment near expiry. | Exit-by-DTE rule | Exits before 0–1 DTE danger | premium-sell | [D] |
| J5 | **Adjustment / roll plan** | Pre-decided response to the strike being tested. | Defined roll/close trigger | Plan exists | both | [D] |

---

## Composite quality score (for trade dissection)
For each trade, score every *applicable* factor +1/0/−1 relative to its structure, then summarize:
- **Factor score** = Σ applicable factor points.
- **Category profile** = net sign per category (A–J) → shows *where* a trade was strong/weak.
- **Coverage** = how many factors were [D]/[M] vs [X] (so we never confuse "scored 0" with "couldn't measure").

The pattern hunt (part 4) then asks: **which factors / category profiles separated the 45 winners from the 36 losers in broker-truth P&L** — with the standing caveat that n = 81 (a net-losing sample) means any resulting formula is a *hypothesis to validate out-of-sample*, never a deployable rule on its own.

## Known-edge anchors (what the literature & this project actually support)
- **Premium selling works only when paid for it:** IV Rank / VRP (C2/C3) is the highest-evidence lever. Selling cheap vol is the most common avoidable mistake.
- **Manage winners, cap losers (J2/J3):** the bot-B post-mortem shows payoff asymmetry, not win rate, sank it.
- **Avoid holding short premium through earnings/events (D1):** the cheapest blowup to eliminate.
- **Flow direction is NOT an edge here (H1/H2):** disproven OOS; demote to context.
- **Structural > predictive:** dealer gamma (H4), term structure (A3/C5), weekend theta (A8) are better-supported than anything requiring directional prediction.
