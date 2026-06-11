# Shortlist validation backtests — 2026-06-11 (FINAL)

Decisive validation runs for the research shortlist
(`research/2026-06-11-options-strategy-research.md`) on the mechanical-replay
harness (`sim_mech.py` + `strategies_shortlist.py`). Window 2025-03-03 →
2026-02-27; hold-out split H1 ≤ 2025-08-28 < H2. Portfolio: $10k, 5% risk
sizing vs max loss (min 1 contract), max 3 concurrent, $0.65/leg/contract RT
commission — harness defaults throughout.

> STATUS: COMPLETE. All option-bar data pulled via Databento OPRA (2,853
> contract-day series, $28.70); all strategies replayed 100% cache-hot
> (deterministic, reproducible, zero fresh API in final runs).
>
> **Headline: one strategy is a VALIDATED-CANDIDATE — S2b, the managed weekly
> SPY put spread (PF 1.86 full-year; PF ≥ 1.35 in BOTH halves at BOTH cost
> levels; max DD $481 on $10k). One conditional candidate (S1a condor gated
> by IV−RV > 2) passes with weaker evidence. Everything else fails or is
> underpowered. 0DTE failed catastrophically at realistic costs, exactly as
> the academic literature predicted.**

## STEP 0 — the actual IV−RV premium in the window (`ivrv_series.csv`)

Per-session series: VIX close (243 archive-cached closes) and SPY median alert
IV (whalestream, biased high) vs trailing 21-session realized vol from daily
closes (annualized, vol points). Both spreads are as-of close; any entry
filter uses the prior session's value (point-in-time).

| month | VIX−RV mean | median | alertIV−RV mean | median | VIX med | RV21 med |
|---|---|---|---|---|---|---|
| 2025-03 | +1.68 | +0.43 | +5.53 | +5.64 | 20.5 | 19.7 |
| 2025-04 | **−17.98** | **−23.22** | −9.61 | −20.20 | 25.4 | **50.4** |
| 2025-05 | −5.74 | −1.19 | −7.13 | −3.07 | 20.8 | 21.5 |
| 2025-06 | +7.00 | +8.17 | +3.35 | +4.59 | 20.8 | 13.0 |
| 2025-07 | +10.86 | +11.14 | +6.58 | +6.42 | 19.4 | 8.9 |
| 2025-08 | +8.63 | +9.31 | +4.44 | +4.14 | 19.9 | 10.5 |
| 2025-09 | +10.36 | +10.64 | +6.95 | +6.84 | 19.5 | 8.8 |
| 2025-10 | +8.60 | +8.05 | +5.53 | +5.91 | 19.8 | 12.8 |
| 2025-11 | +6.31 | +6.54 | +4.68 | +3.57 | 20.2 | 14.4 |
| 2025-12 | +7.29 | +6.89 | +3.30 | +2.81 | 18.8 | 11.9 |
| 2026-01 | +9.18 | +9.36 | +5.81 | +5.89 | 18.7 | 9.0 |
| 2026-02 | +7.75 | +7.80 | +5.71 | +5.75 | 20.3 | 12.6 |

- FULL: VIX−RV mean **+4.65**, median +7.32; days >0: 86%, >2: 82%, >4: 77%.
- H1 (Mar–Aug): mean **+0.98**, median +5.38 — the April crash put realized
  vol at ~50 while VIX peaked then mean-reverted; the premium was **deeply
  negative for ~2 months** (Apr mean −18, May −5.7).
- H2 (Sep–Feb): mean **+8.23**, median +7.86 — **100% of days positive, 99%
  above the +2 filter threshold**.

Two consequences for everything below:
1. The window genuinely contains both failure regimes the research warned
   about: a vol-spike/crash (H1) and a quiet grind-up where the premium was
   fat but call-side structures get run over (H2).
2. The S4 IV−RV filter (>2 pts) is effectively **only active in H1** (15/25
   Mondays pass vs 23/23 in H2). In H2 it is a no-op, so S4-vs-unfiltered is
   an H1 story by construction, and the filter's H2 "performance" is not
   independent evidence.

Measure note: the harvestable premium here (VIX−RV ≈ +4.6 mean over the year,
+8 in calm halves) is in line with the long-run ~+4.2 figure from the
research; 2025-26 was not a premium-starved year — strategies that fail here
are failing for structural/cost reasons, not for lack of premium.

## Strategy implementations (STEP 1)

All in `strategies_shortlist.py` (sim_mech.py untouched). Proxy/disciplines
documented there; key choices:

- Weekly entries fire Mondays (48 of 52; 4 holiday Mondays skipped, no
  make-up entry). Monthly entries = first session after each 3rd-Friday
  expiration (11 cycles; the Feb-2026 cycle expires past the window and is
  dropped).
- Hold-to-expiry orders fetch option bars for the entry day only (exits are
  intrinsic settlements either way; saves ~80% of API budget).
- Monthly structures snap strikes to the $5 grid and use a 60-min fill
  window: verified that $1-grid deep-OTM monthly wings often have zero
  prints (2025-06-23: SPY250718C647 = 0 bars all day; C645 = 20 bars).
- S4 = entry-time-only post-filter on the unfiltered S1a/S2a logs
  (prior-session VIX−RV > {0,2,4}); exact because those strategies never
  hold overlapping positions and the filter can't alter in-trade behavior.
- Cost scenarios: (i) baseline empirical half-spread ($0.05/leg SPY/QQQ),
  (ii) 2×, (iii) S5 only: academic effective spread = 5% of premium
  (split entry/exit). NOTE: on the structure's net credit, (iii) is far
  CHEAPER than (i) — our empirical floor already charges a 12Δ 0DTE spread
  ~$0.20 RT on a ~$0.30 credit. Scenario (iii) therefore bounds S5 from the
  optimistic side, not the pessimistic one.

## Results (STEP 2)

All cells: n / WR / PF / P&L on $10k. H1 = Mar–Aug 2025 (contains the April
crash); H2 = Sep 2025–Feb 2026 (calm grind, fat premium). slip2x = doubled
half-spread per leg.

| strategy / scenario | FULL | H1 | H2 | maxDD |
|---|---|---|---|---|
| S1a condor weekly | 39 / 69% / 0.82 / −857 | 21 / 57% / 0.52 / −1,809 | 18 / 83% / 1.95 / +951 | 3,016 |
| S1a condor weekly 2× | 39 / 69% / 0.67 / −1,637 | 0.43 / −2,229 | 1.56 / +591 | 3,428 |
| S1b condor monthly | 11 / 82% / 0.59 / −1,652 | 0.21 / −3,191 | inf / +1,539 | 4,062 |
| S2a putspread weekly (hold) | 47 / 74% / 0.97 / −186 | 0.94 / −234 | 1.02 / +48 | 1,991 |
| S2a 2× | 47 / 74% / 0.91 / −656 | 0.89 / −474 | 0.94 / −182 | 2,321 |
| **S2b putspread weekly MANAGED** | **47 / 81% / 1.86 / +1,709** | **24 / 79% / 1.77 / +919** | **23 / 83% / 2.00 / +790** | **481** |
| **S2b 2×** | **47 / 77% / 1.39 / +861** | **1.42 / +546** | **1.35 / +315** | **501** |
| S3 bfly monthly | 11 / 64% / 1.82 / +2,976 | 6 / 33% / 0.42 / −2,093 | 5 / 100% / inf / +5,069 | 2,093 |
| S5 SPY 0DTE putspread | 241 / 50% / 0.37 / −4,023 | 0.47 / −1,484 | 0.30 / −2,539 | 4,221 |
| S5 2× | 180 / 22% / 0.09 / −6,723 | 0.10 | 0.08 | 6,746 |
| S5 5%-of-premium (optimistic bound) | 249 / 82% / 1.08 / +442 | 1.52 / +1,150 | 0.80 / −708 | 1,073 |
| S5q QQQ 0DTE (all scenarios) | PF 0.36 / 0.08 / 0.91 | — | — | ≤8,530 |
| S4: condor + IV−RV>0 | 31 / 81% / 1.66 / +1,414 | 1.40 / +463 | 1.95 / +951 | 870 |
| **S4: condor + IV−RV>2** | **30 / 83% / 1.91 / +1,704** | **12 / 83% / 1.86 / +752** | **18 / 83% / 1.95 / +951** | **870** |
| S4: condor + IV−RV>2, 2× | 30 / 83% / 1.56 / +1,104 | 1.56 / +512 | 1.56 / +591 | 1,043 |
| S4: condor + IV−RV>4 | 26 / 81% / 1.53 / +988 | 1.18 / +156 | 1.83 / +833 | 870 |
| S4: putspread(hold) + IV−RV>2 | 38 / 79% / 1.35 / +1,496 | 2.23 / +1,448 | 1.02 / +48 | 1,933 |

Monthly P&L profiles: S2b was profitable in **10 of 12 months** (worst −$274,
Feb-26); S1a lost in 5 of its first 6 months then recovered; S3's entire
profit is 5 calm-half months; S5 lost in 12 of 12 months.

## Verdicts (STEP 3)

Gate: PF ≥ 1.3 in BOTH halves at baseline costs = VALIDATED-CANDIDATE. Any
PF ≥ 2.0 sighting = presumed-overfit unless it survives both halves + 2× costs.

| strategy | verdict | basis |
|---|---|---|
| **S2b managed weekly SPY put spread** (Mon 10:00 ET, short ~40Δ put, $10 wing, ≥4 DTE weekly; TP 50% credit, stop 2× credit, exit 1 DTE) | **VALIDATED-CANDIDATE** | PF 1.77/2.00 by half at baseline; 1.42/1.35 at 2× costs; DD $481; 10/12 months green; n=47. The ONLY arm passing every gate. Management rules were pre-declared from the research, not swept — selection bias is limited (one of two pre-registered variants). |
| **S4 condor gated by IV−RV > 2** | CONDITIONAL CANDIDATE | Passes both halves at both cost levels (1.86/1.95; 1.56/1.56), and threshold neighbors (0, 4) are also positive (not a spike). BUT the filter only binds in H1 (15/25 entry days pass vs 23/23 in H2) — its H2 column is just the unfiltered condor in a friendly regime; real filtered evidence is 12 H1 trades. Promote only as a second paper arm, not a build commitment. |
| S2a hold-to-expiry put spread | FAIL (flat) | PF 0.97 — the premium is real but expiry-tail givebacks eat it; management is where the margin lives. |
| S1a/S1b condors unfiltered | FAIL | Crash-half kills them (PF 0.52 / 0.21 in H1). |
| S3 monthly butterfly | UNDERPOWERED + regime-split | Full PF 1.82 is 5 calm months of H2 doing all the work (H1 PF 0.42); n=11. Do not build. |
| S5/S5q 0DTE credit spreads | **FAIL — decisively** | PF 0.37 (SPY) / 0.36 (QQQ) at empirical costs; even the optimistic 5%-of-premium bound only reaches 1.08 full / 0.80 in H2. Replicates the academic retail-loss findings. Closes the 0DTE question. |

## Honest framing vs the PF ≥ 2.0 mandate

S2b's full-year PF is 1.86 (H2 half exactly 2.00). It does NOT robustly clear
PF 2.0 — and per the evidence review, nothing verified ever has. What it does
do: positive expectancy in a crash half AND a calm half, intact under doubled
costs, with a max drawdown under 5% of the account. On $10k at 5% risk
sizing it earned +17.1%/yr at baseline costs (+8.6% at 2×). That is a real,
compoundable result, not a lottery ticket.

Validation debt that remains before live money: (1) n=47 is one year of one
underlying — extend the backtest 1–2 more years via Databento (~$30–60) to
cover 2023–24 regimes; (2) delta proxy ±0.04 — rerun strike selection against
real chain greeks on the extended data; (3) paper-trade the exact production
implementation per the staged-gate protocol before funding.
