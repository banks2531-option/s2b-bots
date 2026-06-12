# 45-DTE practitioner playbook validation — 2026-06-11 (FINAL)

Decisive test of the canonical tastytrade-style 45-DTE mechanics against the
cached year (2025-03-03 → 2026-02-27; H1 ≤ 2025-08-28 < H2), on the
mechanical-replay harness (`sim_mech.py` → `strategies_shortlist.ShortlistSim`
→ `scale_s2b.EquitySim` → new `strategies_45dte.Sim45`). Portfolio: **$20k,
5% of current equity vs structure max loss (min 1 contract), max 3
concurrent**, harness commissions ($0.65/leg/contract RT) + empirical
half-spread ($0.05/leg SPY/QQQ/IWM), plus a 2×-cost scenario per arm.
Benchmark: the validated **S2b** Monday weekly managed put spread
(PF 1.86 full / 1.77 H1 / 2.00 H2; 1.39 at 2×; maxDD 2.2%; **$144/mo** on
$20k @5%).

> STATUS: COMPLETE. ZERO Databento, droplet untouched. Data: **1,981 fresh
> free-Polygon calls** (12 IWM underlying-month prefetch + 1,969 option
> contract-days), fetched in **12.5 min** (key ran unthrottled again;
> pre-fetch estimate written to `fetch_45dte.log` was 6.56 h at the throttled
> 5/min — inside the 14 h cap, so no scope cuts). 0 failed requests; 69
> genuinely-empty contract-days cached as `[]`. All replays 100% cache-hot
> (`fresh_api_calls: 0` in every summary).
>
> **Headline: the canonical 45-DTE playbook does NOT beat validated S2b on
> this data, and nothing here supports a robust PF ≥ 2.0. The IVR ≥ 30 gate
> is decisively HARMFUL (P1 fails its own control P4). The ungated control
> P4 is the only arm that passes every formal gate — but with double S2b's
> drawdown, half its trade count, and much weaker cost robustness. The
> condor (P2) went 12/12 but is underpowered to meaninglessness. P3
> (multi-underlying IVR harvest) dies at 2× costs.**

## Implementation (strategies_45dte.py — harness files untouched)

- **Entries**: Mondays 10:00 ET (first completed 5-min bar ≥ 10:00), 48
  Mondays in window, 47 usable (2026-02-23 dropped: its 21-DTE exit falls
  past the data window and cannot be observed).
- **Expiry selection**: all holiday-adjusted Fridays with DTE 35–55; the
  **monthly (3rd Friday) is preferred whenever one is in range** (33/47
  Mondays), else the weekly Friday closest to 45 DTE (14/47). Realized entry
  DTE 38–53.
- **Structures**: P1/P3/P4 bull put spread, short ~30Δ, wing **$15** below
  (within the prescribed $15–20; $5 for IWM, price-scaled ≈ 230/640 × 15).
  P2 iron condor, short ~20Δ both sides, $15 wings. All strikes snapped to
  the **$5 grid** (s1b lesson: deep-OTM 30–55-DTE SPY liquidity clusters on
  $5 multiples; $1-grid wings often print zero bars) and entry fill window
  widened to 60 min (s1b convention).
- **Management**: TP at 50% of credit; time exit at 21 DTE (first bar of the
  first session with DTE ≤ 21); P1/P3/P4 stop with harness `stop_mult=2.0`
  (buyback at 3× credit ⇒ realized loss ≈ 2× credit — the same convention
  the validated S2b used for "stop 2× credit"); P2 has **no stop** (condor
  max loss is the stop — house style).
- **IVR gate**: the existing `iv_daily` series (median whalestream alert IV
  per session) with the `sim_proposed` 60-session percentile machinery,
  point-in-time: rank of the prior session's value within the trailing 60
  sessions strictly before the entry date, ≥ 20 sessions of history required.
  Alert-IV is biased high in *level*; harmless for a self-referential rank.
- **Gate stats** (47 usable Mondays): first 4 Mondays have no IVR history
  (window-start). **P1/P2 gate (SPY ≥ 30) passes 20** (H1 7 / H2 13).
  **P3 (highest of SPY/QQQ/IWM ≥ 30) enters 27** (H1 11 / H2 16; picks:
  IWM 13, QQQ 8, SPY 6; ties resolve to the later symbol in SPY→QQQ→IWM
  order, i.e. IWM). The H1 gate-pass Mondays are almost exactly the
  March–April crash weeks (IVR spikes *because* vol already spiked).
- **Delta proxy**: realized put shorts 0.268–0.334 vs 0.30 target; condor
  shorts 0.186–0.217 vs 0.20 — the alert-IV sigma proxy lands on target
  within the known ±0.04.
- **Credits**: put spreads avg $2.55–2.60 on $15 width (17% of width —
  textbook for 30Δ/45 DTE); condor avg $2.76; IWM $0.92–1.15 on $5 width
  (qty 2 from sizing). Avg hold 16–18 calendar days (condor 23 — no stop,
  fewer TPs); TP exits 62–72% of put-spread trades, 25% of condors.

### Data approximations (documented honestly)

1. **Every-other-day bar sampling for the middle of the hold** (authorized):
   option bars fetched for the entry session, every 2nd session after, and
   ALWAYS the 21-DTE boundary session + the following session, so the time
   exit resolves on the correct day. TP-50% and stop checks therefore run on
   ~every-other-day marks; an exit that would have triggered on a skipped
   day resolves on the next fetched day (delays both winners' TPs and
   losers' stops by ≤ 1 session; roughly symmetric, small). Every trade
   still had ≥ 20 intersection-aligned marks (none thin).
2. **45-DTE weekly Fridays often did not exist/print**: all 51 SPY/IWM empty
   contract-days sit on non-monthly Friday expiries (06-06, 07-03, 08-01,
   09-05, 10-03, 11-07, 12-05, 02-06) — at 45 days out those weeklies are
   barely listed. ~5 weekly-pick Mondays per put-spread arm dropped as
   `no_pricing` (e.g. 4/21, 5/19, 7/21, 9/22, 10/20, 12/22). This is a
   listing/liquidity artifact, not outcome-conditioned; a live trader would
   simply use the monthly. Of P4's 36 fills, 30 are monthly cycles.
3. **P2 missed the two ugliest vol Mondays by a grid artifact**: on 4/7 and
   4/14 (IVR 96/80) the 20Δ *call* sat beyond the synthetic ±12% strike
   grid → `no_long_strike` reject. The condor's record is therefore missing
   exactly the entries most likely to have hurt it — its results are biased
   FAVORABLY. Flagged below.
4. One P3 entry (12/1, QQQ Jan-monthly) dropped because the QQQ $595/$575
   strikes genuinely printed nothing on the entry day (QQQ monthly liquidity
   is far patchier than SPY's).
5. Entry-count accounting per arm: P1 16 of 20 gate-passes (2 `no_pricing`,
   2 `max_concurrent`); P4 36 of 47 (1 `no_iv` first Monday, 5 `no_pricing`,
   5 `max_concurrent`); P2 12 of 20 (2 `no_long_strike`, 4 `no_pricing`,
   2 `max_concurrent`); P3 21 of 27 (3 `no_pricing`, 3 `max_concurrent`).
   `max_concurrent` rejects are real playbook behavior (3-4-week holds ×
   weekly entries × cap 3), not artifacts.
6. At 2× costs the smaller net credits lower the 3×-credit stop trigger, so
   two trades flip TP/time-exit → stop and one freed slot admits an extra
   2025-11-24 entry (n 16→17, 36→37). Verified mechanically; not a bug.

## Results

All cells: n / WR / PF / P&L on $20k @ 5% equity-risk. H1 contains the April
2025 crash; H2 is the calm grind. 2× = doubled per-leg half-spread.

| arm / scenario | FULL | H1 | H2 | $/mo | maxDD ($, %) | worst wk |
|---|---|---|---|---|---|---|
| **P1 putspread IVR≥30** | 16 / 81% / 1.76 / +619 | 6 / 83% / **0.99** / −10 | 10 / 80% / 4.94 / +629 | 52 | 656 / 3.3% | −656 |
| P1 2× | 17 / 65% / **0.70** / −526 | **0.55** / −444 | **0.89** / −82 | −44 | 845 / 4.2% | −555 |
| **P4 putspread NO gate (control)** | 36 / 89% / **2.79** / +2,385 | 20 / 90% / **2.02** / +1,194 | 16 / 88% / **8.46** / +1,191 | **201** | 1,176 / 5.8% | −1,176 |
| P4 2× | 37 / 78% / 1.35 / +819 | 1.33 / +498 | 1.41 / +321 | 69 | 1,384 / 6.8% | −1,080 |
| P2 iron condor (no stop) | 12 / 100% / inf / +1,063 | 3 / inf / +238 | 9 / inf / +825 | 90 | 0 / 0% | +54 |
| P2 2× | 12 / 92% / 33.4 / +603 | inf / +122 | 26.8 / +481 | 51 | 10 / 0.05% | −10 |
| **P3 multi-underlying IVR** | 21 / 71% / 1.90 / +822 | 9 / 78% / 1.35 / +204 | 12 / 67% / 2.83 / +618 | 69 | 545 / 2.7% | −349 |
| P3 2× | 21 / 62% / **0.91** / −138 | 1.13 / +72 | **0.78** / −210 | −12 | 720 / 3.5% | −553 |
| *S2b benchmark ($20k @5%)* | *47 / 81% / 1.86 / +1,709* | *1.77* | *2.00* | ***144*** | *481 / 2.2%* | *−481* |
| *S2b 2×* | *47 / 77% / 1.39 / +861* | *1.42* | *1.35* | *73* | *501 / 2.4%* | *−501* |

Monthly P&L: P4 was green in 10/12 months (−$1,012 Apr-25 — both crash
stops settle in week 2025-W14 — and −$160 Feb-26 window-truncated time
exits); P1 green in 6/8 active months; P3 green 8/10; P2 green 7/7 active
months (but see verdict). Exit mix (P4 baseline): 26 TP / 8 time-exit-21DTE
/ 2 stop.

## Verdicts (gates: PF ≥ 1.3 both halves at baseline = pass; any PF ≥ 2.0 =
presumed overfit unless it survives both halves AND 2× costs)

| arm | verdict | basis |
|---|---|---|
| **P1 SPY 45-DTE putspread, IVR ≥ 30** | **FAIL** | H1 PF 0.99 at baseline (gate breach); 0.70 at 2×. The gate selects exactly the crash Mondays in H1 (3/31, 4/7, 4/14, 4/28 …) and blocks 16 profitable calm Mondays. n=16 additionally underpowered. |
| **P4 control (P1 without the gate)** | **PASS (the only one) — but does NOT beat S2b** | 2.02/8.46 by half at baseline, 1.33/1.41 at 2× — formally survives every gate including the PF≥2 overfit-rebuttal rule. See honest framing below. |
| **P2 SPY 45-DTE iron condor** | **UNDERPOWERED / NOT VALIDATED** | n=12, WR 100%, PF inf, maxDD $0 — too few trades for inference; missing the two worst-vol entries via the grid artifact (favorable bias); the 3/31 entry rode the April crash to +$77 purely because the V-recovery arrived before the 21-DTE exit (no stop = pure path luck on n=1 crash). Do not promote on this evidence. |
| **P3 multi-underlying IVR harvest** | **FAIL (cost-fragile)** | Passes halves at baseline (1.35/2.83) but flips sign at 2× (0.91 full, 0.78 H2) — the same failure mode as the scaling study's Friday/QQQ arms: thinner per-trade margins (IWM credits ~$1.05 ×2 qty double the friction) that cost-doubling erases. |

## Honest framing

**Does the canonical 45-DTE playbook beat validated S2b on this data?** No.
The faithful playbook (P1, WITH its IV-rank condition) loses outright — the
IVR ≥ 30 gate repeats the legacy IVR ≥ 35 weekly finding on 45-DTE cycles:
IV rank is high right after vol explodes, so the gate concentrates short-put
entries into the crash and starves the strategy in the calm half (7 of its
20 passes are March–April crash Mondays; it then skips most of the regime
where the premium was fattest). The gate-stripped control P4 is genuinely
good — and that itself is the cleanest finding: **on this data the 45-DTE
edge, like the weekly edge, comes from selling SPY puts mechanically and
managing early, not from IVR conditioning.**

P4 vs S2b: +$201/mo vs +$144/mo at baseline, but P4 carries 2.4× the
drawdown (5.8% vs 2.2%), one-third fewer independent trades (36 vs 47, with
longer overlapping holds — worst week −$1,176 vs −$481), and its baseline
costs are LESS trustworthy: the $0.05/leg empirical half-spread was
calibrated on 4–25-DTE contracts, while 45-DTE far-OTM wings trade wider —
the truth for this playbook sits somewhere between the baseline ($201/mo)
and 2× ($69/mo) columns, plausibly nearer the middle, i.e. roughly S2b's
neighborhood with worse tails. At 2× costs P4 (1.35 PF, $69/mo) is no
better than S2b (1.39, $73/mo). **Nothing here justifies replacing or
diluting the S2b build; at most P4 earns a paper-trade slot as a
lower-frequency sibling.**

**Does anything support PF ≥ 2.0?** No. Every PF ≥ 2.0 cell is either the
calm half doing all the work (P4 H2 8.46 = 14 wins vs two small
window-truncated losses; P3 H2 2.83), an n=12 perfect-record condor whose
two scariest entries were excluded by a proxy artifact, or a headline (P4
full 2.79) that collapses to 1.35 the moment costs double. P4 technically
passes the letter of the overfit-rebuttal rule at the ≥1.3 survival level,
but the PF-2 *magnitude* itself is not cost-robust anywhere. This matches
every prior result in the program: real, modest, manageable edge ≈ PF
1.3–1.9; PF ≥ 2.0 remains unverified on honest costs.

## Artifacts

`strategies_45dte.py` (strategies + Sim45 + enumerate/fetch/run modes),
`misses_45dte.json` (1,969 contract-days, per-strategy attribution +
pre-fetch wall-clock estimate), `fetch_45dte.log` (estimate, pacing, 0
failures), `trades_45dte_<tag>.csv` + `summary_45dte_<tag>.json` × 8 (full
trade logs, halves, monthly P&L, exit reasons, rejects, coverage — all
`fresh_api_calls: 0` on the recorded runs). Gate table reproducible via
`python strategies_45dte.py --gate-stats`. Nothing committed; droplet and
E:\ archive untouched.
