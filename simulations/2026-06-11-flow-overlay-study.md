# Flow-as-overlay study — does UW/whalestream flow condition S2b? (2026-06-11)

**Question.** Flow as a directional/origination signal already failed OOS
decisively (research/2026-06-10-uw-flow-edge-study.md: ~82 slices, 5/5
pre-registered hypotheses refuted). This study asks the narrower question:
does morning index/macro flow tone add value as a **VETO or SIZING overlay**
on the validated S2b schedule (SPY bull put spread, Mon 10:00 ET, short ~40Δ
put, $10 wing, ≥4 DTE weekly, TP 50%, stop 2×, exit 1 DTE; PF 1.86 full /
1.77 H1 / 2.00 H2)?

**Answer up front: NO OVERLAY EDGE FOUND.** Zero of the 9 computable
pre-registered features passed the three-gate panel test at baseline costs
(or at 2× costs). Worse for the subscription case: the index-put-flow
features mostly ran **opposite** to the pre-registered direction — mornings
heavy in index put buying were the *best* mornings to sell SPY put spreads —
and that reverse effect is itself half-unstable, so it cannot be flipped
into an overlay either. The UW subscription adds no measurable value to S2b
even as a filter. Details below.

Constraints honored: zero Databento; data = existing local cache + free
Polygon key (966 contract-days fetched, ran unthrottled again, 5.6 min) +
droplet read-only + local whalestream archives. No droplet state touched.

---

## 1. Block / dark-pool data inventory (new)

| dataset | where | coverage | size/rows | fields | status |
|---|---|---|---|---|---|
| `whalestream_darkpool_merged.csv` | LOCAL `C:\Users\banks\trading-bot\data\` (+ raw `whalestream_darkpool/` 393 daily JSONs, also in `E:\BanksBackup`) | 2025-03-01 → 2026-03-28 | 229 MB / 2,728,586 dark prints (of 2,906,481 records; lit-block rows dropped by the merge but recoverable from raw JSONs) | date(UTC), symbol, is_dark, premium, size, price, bid, ask, bid_ask_indicator, volume | **USABLE.** Indicator present on 64% of rows. EQUITY prints — no put/call dimension exists, so "block put share" is **not computable**; closest analogs used instead. |
| Block alerts (`whalestream_block_alerts_pull.py`) | raw + merged outputs **DELETED** | was 2025-03-01 → 2026-03-01 | was 183 files / 1,728,167 deduped rows, 26 cols | per `merge_dedupe_block_alerts.py`: flattened alert dicts (exchange, is_large, is_signature_print, bid/ask, asset.*) | **GONE.** Only `whalestream_block_alerts_merge_summary.txt` + one raw 2-day sample in `E:\BanksBackup\Downloads` survive. Droplet has only scripts/log copies. Documented absence. |
| `whalestream_options/` daily JSONs | droplet (read-only) | 2025-03-03 → 2026-02-27, 260 files, 3.4 GB | 1,849,446 raw → **1,036,006 unique** after uuid dedupe (reduced on-droplet to `flow_overlay_reduced.csv.gz`, 9 MB) | utc, sym, cls(stock/etf/index), C/P, premium, bid_ask_indicator, sweep | USABLE; full ticker universe (unlike the local WATCHLIST-filtered CSV, which also contains pull-overlap duplicates). |
| SPY/QQQ dark prints in the 04:00–09:59 ET window | (subset of darkpool CSV) | 627 prints all year, only 80 with a bid/ask indicator, median 2/day | | | Feature F6 **never reaches the pre-registered ≥5-indicator-print floor → documented absent**. 9 of 10 features computable. |

## 2. Panel construction + coverage

- Daily S2b entries Mon–Fri 10:00 ET, 2025-03-03 → 2026-02-27, same harness
  as the validated runs (`panel_data.py` two-pass enumerate→fetch,
  `run_panel.py`; ShortlistSim, MAX_CONCURRENT=20, daily halt disabled,
  fixed 5% risk → qty 1/trade, identical fills convention).
- Fetch: 966 missing (contract, ET-day) keys enumerated network-off; all 966
  fetched OK (0 empty, 0 failed) in 5.6 min — the free key again ran
  unthrottled. Final replay 100% cache-hot (0 fresh calls).
- **Panel: n=246** (Mon 47 / Tue 52 / Wed 49 / Thu 48 / Fri 50; rejects: 1
  `no_iv`, 3 `no_pricing`). H1 n=122 / H2 n=124. Exits: 169 TP, 31 stop, 46
  time-exit.
- Panel-level finding worth keeping: unconditioned daily-entry PF is only
  **1.032 full (1.168 H1 / 0.906 H2)** at baseline costs, 0.762 at 2× —
  S2b's documented edge is concentrated in the Monday entry (and partly
  Friday, cf. scaling study v1_fri PF 1.42 vs v1_wed 1.08). The panel is for
  feature inference, not a tradable arm. Panel trades overlap in time → not
  independent; gates lean on H1/H2 consistency, not t-stats.
- Checkpoints: `trades_panel_daily.csv`, `trades_panel_daily_slip2x.csv`,
  `flow_features.csv`, `flow_overlay_results.json`, `panel_misses.json`,
  `fetch_panel.log`.

## 3. Pre-registration (verbatim from `flow_overlay_prereg.md`, written before any outcome join)

Timing: AM window = 04:00–09:59:59 ET on entry day D; PRIOR window =
04:00–19:59:59 ET on the prior session. INDEX_SET = {SPY, SPX, SPXW, QQQ}.
Ask-side = indicator ∈ {A,AA,TA}; bid-side = {B,BB,TB}; bearish = put@ask +
call@bid; bullish = call@ask + put@bid.

| # | feature | definition | hostile when |
|---|---|---|---|
| F1 | idx_put_share_am | INDEX_SET put premium / total INDEX_SET premium, AM | HIGH |
| F2 | idx_put_ask_prem_am | log10(1+$ ask-side INDEX_SET put premium), AM | HIGH |
| F3 | idx_put_sweep_cnt_am | INDEX_SET put sweep count, AM | HIGH |
| F4 | breadth_bear_am | share of single-name stocks with bearish-dominant AM flow (NaN if <10 tickers) | HIGH |
| F5 | dp_prem_am | log10(1+total dark-pool premium, AM) | HIGH |
| F6 | dp_idx_sell_share_am | SPY+QQQ dark-print bid-side premium share, AM (NaN if <5 indicator prints) | HIGH |
| F7 | idx_put_share_prior | F1 over PRIOR window | HIGH |
| F8 | idx_put_ask_prem_prior | F2 over PRIOR window | HIGH |
| F9 | dvix | VIX close(D−1) − VIX close(D−2) (control, not flow) | HIGH |
| F10 | alert_cnt_am | total deduped alert count, all tickers, AM | HIGH |

Gates (declared): a feature PASSES only if (a) removing its hostile quartile
raises PF vs the unconditioned panel in BOTH halves; (b) the improvement
survives removing the single best-saved trade; (c) the hostile bucket's mean
P&L is below the panel mean in BOTH halves. Production tests (47 real Monday
trades, both cost levels): VETO at the H1-p75 threshold (H1-fit, H2-frozen);
SIZING 1.5×/1.0×/0.5× by H1-fit terciles. Missing feature ⇒ trade normally.
Multiplicity: ~60+ comparisons ⇒ expect 1–2 false positives at |t|≈2; one
passing feature = candidate for paper validation only.

## 4. Panel bucket tables — baseline costs (n=246, uncond PF 1.032 / H1 1.168 / H2 0.906)

Q4 = pre-registered hostile quartile. Cells: mean$ / PF (n per bucket ≈ 61;
per-half n 13–49 as shown in `flow_overlay_results.json`). t = Welch t of
hostile vs rest (full).

| feature (cuts q1/q2/q3) | Q1 full | Q2 full | Q3 full | Q4 full | Q4 H1 | Q4 H2 | kept-PF H1 vs uncond | kept-PF H2 vs uncond | t | gates a/b/c |
|---|---|---|---|---|---|---|---|---|---|---|
| F1 idx_put_share_am (.49/.62/.72) | −44 / 0.60 | +2 / 1.03 | +21 / 1.29 | **+34 / 1.51** | +15 / 1.17 | +48 / 1.93 | 1.20 vs 1.19 | 0.69 vs 0.91 | 1.32 | F/F/F |
| F2 idx_put_ask_prem_am (7.04/7.28/7.52) | −17 / 0.82 | +22 / 1.39 | −6 / 0.94 | +14 / 1.16 | −28 / 0.77 | +64 / 2.53 | 1.51 vs 1.19 | 0.71 vs 0.91 | 0.42 | F/F/F |
| F3 idx_put_sweep_cnt_am (24/33/55) | −35 / 0.67 | +12 / 1.18 | +17 / 1.25 | +20 / 1.23 | −43 / 0.70 | +108 / 8.79 | 1.68 vs 1.17 | 0.63 vs 0.91 | 0.68 | F/F/F |
| F4 breadth_bear_am (.43/.48/.53) | +25 / 1.38 | +1 / 1.01 | −4 / 0.96 | −7 / 0.93 | −20 / 0.80 | +11 / 1.15 | 1.46 vs 1.21 | 0.85 vs 0.91 | −0.43 | F/F/F |
| F5 dp_prem_am (9.61/9.71/9.81) | +15 / 1.21 | +11 / 1.15 | +1 / 1.01 | −16 / 0.84 | +34 / 1.42 | −31 / 0.72 | 1.13 vs 1.17 | 1.09 vs 0.91 | −0.74 | F/F/F |
| F7 idx_put_share_prior (.42/.55/.65) | −9 / 0.89 | −23 / 0.78 | −0 / 1.00 | **+42 / 1.60** | +69 / 2.23 | +11 / 1.12 | 0.92 vs 1.17 | 0.84 vs 0.91 | 1.65 | F/F/F |
| F8 idx_put_ask_prem_prior (8.09/8.23/8.47) | −26 / 0.72 | −16 / 0.83 | +11 / 1.15 | **+41 / 1.61** | +9 / 1.08 | +86 / 5.68 | 1.22 vs 1.17 | 0.68 vs 0.91 | 1.64 | F/F/F |
| F9 dvix (−1.15/−0.01/+1.13) | +5 / 1.06 | +4 / 1.05 | −10 / 0.90 | +20 / 1.27 | +22 / 1.26 | +18 / 1.28 | 1.22 vs 1.23 | 0.81 vs 0.91 | 0.65 | F/F/F |
| F10 alert_cnt_am (381/489/669) | +34 / 1.60 | +13 / 1.20 | −1 / 0.99 | **−36 / 0.71** | −76 / 0.56 | −2 / 0.98 | **1.79 vs 1.17 ✓** | 0.88 vs 0.91 ✗ | −1.40 | F/F/F |

At 2× costs (uncond 0.762 / H1 0.812 / H2 0.709) the picture is the same;
the only gate-a+c pass anywhere was F5 dp_prem_am at slip2x (kept-PF 0.856
vs 0.812 H1, 0.847 vs 0.709 H2), which then failed gate b (the improvement
does not survive removing one trade) and fails entirely at the registered
baseline cost level. **0 of 9 features pass.**

Two honest readings of the table:
1. The pre-registered direction is mostly WRONG: heavy same-morning or
   prior-day index put buying (F1, F7, F8) sat in the *best* quartiles
   (PF 1.5–1.6 vs 0.6–0.9 in the calm quartiles). Mechanism is presumably
   the one the 2026-06-10 study already identified — institutional put flow
   is hedging noise, and put-rich mornings carry richer credit. **Do not
   flip this into a reverse overlay**: the hostile-quartile sign flips
   between halves for F2/F3 (H1 PF 0.70–0.77 vs H2 2.5–8.8), exactly the
   regime-noise signature that killed the directional study. Recorded here
   so it is not rediscovered and re-overfit later.
2. The only feature whose registered direction had any traction was F10
   (busy alert mornings = bad), and it fails H2 (0.88 vs 0.91). F9 (ΔVIX,
   the non-flow control) fails too — its hostile quartile was *fine* in the
   panel (PF 1.27).

## 5. Production tests on the real 47 Monday trades (H1-fit p75 veto; H1-fit tercile sizing, frozen on H2)

Baseline = PF 1.859 (H1 1.768 / H2 1.996), P&L $1,709, maxDD $481 — at 2×:
PF 1.393 (1.421/1.351), $861, $501. (Sweep across all 9 features = 36
tests; treat any single green cell as multiplicity noise.)

| feature | VETO base: PF (H1/H2), P&L, DD | VETO 2×: PF (H1/H2), P&L | SIZE base: PF (H1/H2), P&L, DD | SIZE 2×: PF (H1/H2), P&L |
|---|---|---|---|---|
| F1 idx_put_share_am | 1.13 (1.20/1.01), $251, $481 | 0.85 (0.93/0.73), −$335 | 1.75 (1.91/1.54), $1,502, $481 | 1.30 (1.50/1.06), $670 |
| F2 idx_put_ask_prem_am | 1.91 (1.97/1.85), $1,460, $481 | 1.38 (1.53/1.25), $677 | 1.84 (1.71/2.05), $1,709, $539 | 1.36 (1.35/1.37), $808 |
| F3 idx_put_sweep_cnt_am | 2.01 (1.89/2.17), $1,473, $481 | 1.49 (1.45/1.55), $788 | 1.74 (1.46/2.22), $1,605, $551 | 1.29 (1.15/1.52), $696 |
| F4 breadth_bear_am | 2.50 (**1.60**/7.37), $1,749, $367 | 1.84 (**1.30**/4.16), $1,098 | 2.22 (**1.68**/3.58), $2,088, $367 | 1.60 (**1.32**/2.25), $1,156 |
| F5 dp_prem_am | 1.73 (**1.17**/19.7), $901, $367 | 1.25 (**0.90**/8.5), $336 | 1.60 (**1.31**/2.73), $1,175, $551 | 1.22 (**1.05**/1.87), $467 |
| F7 idx_put_share_prior | 1.42 (1.42/1.41), $737, $481 | 1.05 (1.08/1.01), $88 | 1.53 (1.47/1.61), $1,194, $539 | 1.12 (1.14/1.10), $302 |
| F8 idx_put_ask_prem_prior | 1.55 (1.47/1.66), $984, $481 | 1.15 (1.13/1.18), $293 | 1.58 (1.47/1.70), $1,397, $722 | 1.16 (1.15/1.18), $435 |
| F9 dvix (control) | 2.23 (**1.51**/6.60), $1,455, $367 | 1.59 (**1.17**/3.55), $779 | **2.49 (2.15/3.19), $2,272, $359** | **1.78 (1.69/1.94), $1,345** |
| F10 alert_cnt_am | 2.13 (**1.44**/6.17), $1,301, $367 | 1.51 (**1.10**/3.58), $655 | 1.70 (1.51/2.31), $1,239, $551 | 1.29 (1.20/1.55), $554 |

Reading: every flow-feature veto either *reduces* P&L for no PF gain, or
buys its full-window PF by gutting H1 (bold cells: H1 PF drops below
baseline — the "improvement" is one H2 tail-save wearing a frozen-threshold
costume). F3's veto is the most benign (+0.15 PF both halves) but its panel
test failed with the hostile quartile at H2 PF 8.79 — same trade direction
as its quartile noise, n_vetoed = 9, not evidence. The single config that
improves both halves at both cost levels is **F9 ΔVIX sizing** (PF 1.86→2.49
base, 1.39→1.78 at 2×) — the control feature that *requires no flow data at
all* — and it FAILED its own pre-registered panel test (hostile ΔVIX
quartile was profitable, PF 1.27). With 36 production cells, one such hit is
within the expected false-positive count we pre-declared. It is at most a
note for some future VIX-conditioning study with fresh data; it is NOT a
validated overlay, and it is NOT a flow edge.

## 6. Multiple-comparison statement

9 features × (4 quartiles × 2 halves panel + veto + sizing × 2 cost levels)
≈ 100+ reported numbers, ~45 decision-relevant comparisons. At |t|≈2 we
expected 1–2 spurious passes; we observed zero full passes and a handful of
partial-gate hits (F5 at slip2x, F10 H1-only, F9 Monday sizing) — i.e., at
or below the noise floor. The pre-registered gates did exactly their job.

## 7. Verdict

**NO OVERLAY EDGE FOUND.** Pre-registered, point-in-time morning flow
features — index put premium share, index put ask-side premium, index put
sweeps, single-name bearish breadth, dark-pool premium, prior-day variants,
and total alert activity — do not identify hostile S2b entry days with any
half-consistent reliability, on a 246-trade daily panel or on the real
47-Monday trade set, at either cost level. The block-alert archive no longer
exists to test; the dark-pool archive exists and also fails. Combined with
the 2026-06-10 directional study, the conclusion is now two-sided: **the UW
subscription adds no measurable systematic value — neither as an entry
signal nor as a veto/sizing filter on S2b.** If it is kept, it is for
discretionary situational awareness, not for the bot. The only sliver that
improved the Monday set in both halves (ΔVIX-based sizing) needs no
subscription, failed the panel gates, and should not be acted on without
fresh out-of-sample data.

### Artifacts (this directory)
- `flow_overlay_prereg.md` (pre-registration, written before joins)
- `flow_overlay_reduced.csv.gz` (1.036M deduped alerts, droplet reduce via `reduce_flow_overlay.py`)
- `flow_features.csv` (250 sessions × 10 features; F6 all-NaN by rule)
- `trades_panel_daily.csv` / `_slip2x.csv` + `summary_panel_daily*.json` (246-trade panel)
- `flow_overlay_results.json` (all bucket/gate/veto/sizing numbers)
- Scripts: `panel_data.py`, `run_panel.py`, `build_flow_features.py`, `flow_overlay_analysis.py`
- `panel_misses.json`, `fetch_panel.log` (966/966 keys fetched, 5.6 min)
