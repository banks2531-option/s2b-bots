# Pre-registration: UW/whalestream flow as a CONDITIONING OVERLAY on S2b — 2026-06-11

Written BEFORE any feature↔outcome join is computed (panel construction and raw
data inventory only have been done at this point; no flow feature has been
joined to any trade P&L). Prior context: flow as a DIRECTIONAL/ORIGINATION
signal already failed OOS decisively (research/2026-06-10-uw-flow-edge-study.md,
5/5 pre-registered hypotheses refuted). THIS study asks a different, narrower
question: does morning index/macro flow tone work as a VETO or SIZING overlay
on S2b's fixed Monday schedule (and on a daily-entry expansion panel)?

## Panel and outcome

- Panel: S2b structure (SPY bull put spread, entry 10:00 ET, short ~40Δ put,
  $10-wide wing, nearest weekly ≥4 DTE, TP 50% credit, stop 2× credit, time
  exit 1 DTE) entered EVERY trading day 2025-03-03 → 2026-02-27, fixed $500
  risk sizing (5% of $10k), max_concurrent raised so no entry is rejected for
  concurrency. Outcome = per-trade P&L at baseline costs ($0.05/leg half-spread,
  $0.65/leg RT commission). H1 = entry_date ≤ 2025-08-28; H2 = after.
- Production set: the validated 47 Monday S2b trades
  (trades_sl_s2b_putspread_managed.csv and its _slip2x twin).

## Timing windows (all point-in-time, strictly before the 10:00 ET entry)

- AM window: events with ET timestamp 04:00:00 ≤ t < 10:00:00 on entry day D.
- PRIOR window: events with ET timestamp 04:00:00 ≤ t < 20:00:00 on the prior
  trading session.
- Event timestamps: whalestream `date` field (UTC) converted to ET. Caveat
  (documented, not fixable): archives are daily REST pulls, so we assume an
  alert was visible live at its own timestamp; dark-pool prints may be
  reported late by FINRA, so their point-in-time availability is optimistic.

## Data sources

- Options flow: droplet `/root/trading-bot/data/whalestream_options/` daily
  JSONs (260 files, 2025-03-03 → 2026-02-27), deduped by `uuid`. Full ticker
  universe, no premium re-floor beyond what the product applies.
- Dark pool / blocks: LOCAL `C:\Users\banks\trading-bot\data\whalestream_darkpool_merged.csv`
  (2.73M dark prints, 2025-03-03 → 2026-03-27, fields incl. premium and
  bid_ask_indicator; 64% of rows have an indicator). The separate
  block-alerts pull (183 raw files, 1.73M merged rows) was DELETED after
  merging — only its summary survives — so "block" features below use the
  dark-pool print archive, which is what exists. These are EQUITY prints:
  there is no put/call dimension, so the pre-specified "block put share" is
  NOT COMPUTABLE; the closest available analogs (premium level, bid-side
  share) are registered instead.
- VIX: `ivrv_series.csv` (archive-cached daily closes).
- INDEX_SET = {SPY, SPX, SPXW, QQQ} (SPXW = SPX weeklys, folded into SPX).
- Direction read (same convention as the 2026-06-10 study):
  ask-side = bid_ask_indicator ∈ {A, AA, TA}; bid-side = {B, BB, TB};
  bearish premium = put@ask + call@bid; bullish = call@ask + put@bid.

## Features (computed per entry day D) and pre-registered HOSTILE direction

| # | feature | definition | hostile to S2b when |
|---|---|---|---|
| F1 | idx_put_share_am | INDEX_SET put premium / total INDEX_SET premium, AM window (NaN if total = 0) | HIGH |
| F2 | idx_put_ask_prem_am | log10(1 + $ ask-side INDEX_SET put premium), AM window | HIGH |
| F3 | idx_put_sweep_cnt_am | count of INDEX_SET put alerts whose specific type contains "sweep", AM | HIGH |
| F4 | breadth_bear_am | among single-name stocks with ≥1 direction-readable alert in AM, share whose bearish premium > bullish premium (NaN if <10 tickers) | HIGH |
| F5 | dp_prem_am | log10(1 + total dark-pool print premium, all symbols, AM) | HIGH |
| F6 | dp_idx_sell_share_am | SPY+QQQ dark prints AM: bid-side premium / (bid+ask side premium) (NaN if <5 indicator-bearing prints) | HIGH |
| F7 | idx_put_share_prior | F1 over PRIOR window | HIGH |
| F8 | idx_put_ask_prem_prior | F2 over PRIOR window | HIGH |
| F9 | dvix | VIX close(D−1) − VIX close(D−2) (control, not flow) | HIGH |
| F10 | alert_cnt_am | total deduped option-flow alert count, all tickers, AM | HIGH |

Rationale for HIGH=hostile everywhere: S2b is short SPY downside; heavy
bearish/put/sell-side positioning or rising vol before entry is the
pre-registered danger state. No LOW-hostile hypotheses are registered.

## Tests and pass gates (declared before computing)

1. Panel bucket test per feature: quartiles by full-panel feature rank
   (above/below median where NaNs or ties make quartiles thin, i.e. <15
   trades per quartile). Report n, mean P&L, WR, PF per bucket, per half.
2. A feature PASSES as overlay candidate ONLY if ALL of:
   a. removing the hostile quartile (Q4) raises PF vs the unconditioned panel
      in BOTH halves;
   b. the improvement survives removing the single best-saved trade (the
      largest loss inside the vetoed set);
   c. the hostile bucket's mean P&L is below the panel mean in BOTH halves
      (same-direction effect).
3. Production tests on the 47 real Monday trades, BOTH cost levels:
   - VETO: skip entries with feature in the hostile zone. Threshold = the
     H1-Monday 75th percentile of the feature (fit on H1 ONLY), applied
     frozen to H2. Report PF/P&L/maxDD delta vs baseline.
   - SIZING: terciles of the feature fit on H1 Mondays; qty multiplier
     1.5× (least hostile) / 1.0× / 0.5× (most hostile), frozen for H2.
4. Multiple-comparison statement: 10 features × (4 quartile buckets + 2
   halves + 2 production tests) ≈ 60+ comparisons; at |t|≈2 expect 1–2
   false positives. A single passing feature is a CANDIDATE for paper
   validation only; two independent features (not mechanically correlated,
   e.g. F2 vs F9) agreeing is stronger evidence.
5. If nothing passes all gates: verdict is NO OVERLAY EDGE FOUND and the UW
   subscription adds no measurable value to S2b even as a filter.

Panel caveat (declared): overlapping multi-day holds share market moves, so
panel trades are not independent; bucket t-stats are indicative, and the
H1/H2 consistency + leave-one-out gates carry the real weight.
