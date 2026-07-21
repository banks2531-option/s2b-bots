# EOD review — 2026-07-21 (supervised dry run)

## Scope and ground truth

- Snapshot: `reports/latest/manifest.json`, generated 2026-07-21 19:23:01 ET, market `CLOSED`, slot `adhoc`.
- Deployed commit: `ea8d533456c513c34a85b645e07650a0f0045e83`.
- Working commit reviewed: `881d84e0dcd0c61e98786d07fd788458f09bac98`.
- Bot B P&L source: `actual_fill` / `sandbox_unaudited`; pre-2026-07-20 history is excluded from clean quantitative inference because of the documented sandbox sign/quantity distortion.
- Bot C P&L source: `synthetic_mark` / `live_fill_accounting_disabled`.
- No production, broker, or external service was contacted. No trading code, branch, configuration, bot, or order was changed.

## Executive result

Bot B realized $293.60 from three take-profit closes of positions opened on Monday, July 20, and ended with two Tuesday entries carrying $20 of sandbox unrealized P&L. Bot C made no entry and retained two positions with $42 of synthetic unrealized P&L. Owned broker legs, quantities, equities, and pending-order counts reconcile for both bots. No position met the review's threat threshold.

## Entry funnel

### Bot B — sandbox

`743 cycles → 679 raw evaluations → 176 unique opportunities → 370 decision rows → 2 fills`

| Decision | Count | Interpretation |
|---|---:|---|
| `duplicate_strikes` | 256 | Repeated cycles after a strike pair was already represented |
| `risk_budget:gap_2atr` | 81 | Deepest gap-stress scenario permitted zero additional contracts |
| `off_hours` | 29 | Entry window closed |
| `filled` | 2 | One contract each |
| `quote_invalid` | 1 | Quote-quality fail-closed behavior |
| `canceled` | 1 | One canceled attempt followed by a later fill; not a repeated-failure pattern |

- 740/730, expiry 2026-07-29, qty 1: filled at 1.71; entry delta 0.2758. The decision row carried executable credit 1.70, ratio 0.17 versus threshold 0.14, and 1.5-ATR aggregate stress 1,924.66.
- 741/731, expiry 2026-07-29, qty 1: filled at 1.61; entry delta 0.2943. A 1.63 attempt was canceled, followed by a fill with decision credit 1.60, ratio 0.16 versus threshold 0.14, and 1.5-ATR aggregate stress 2,005.93.

### Bot C — live

`752 cycles → 690 raw evaluations → 164 unique opportunities → 373 decision rows → 0 fills`

| Decision | Count | Interpretation |
|---|---:|---|
| `risk_budget:total_stop` | 277 | Existing plus incremental stop exposure left zero contract capacity |
| `risk_budget` / `requested_qty` | 67 | Pre-cap requested quantity rounded or adjusted to zero |
| `off_hours` | 29 | Entry window closed |

Bot C's configured total-stop limit is 30% of $891.03, or $267.31. The synchronized CSV does not expose the active binding-cap fields needed to reconstruct each zero-quantity decision exactly; see Finding 4.

## Trade-by-trade causality

### Bot B closes

| Position | Origin | Exit | Realized P&L | Causality |
|---|---|---|---:|---|
| 735/725 x2, 2026-07-24 | Opened 2026-07-20 at 1.31 | TP at 0.65 | +$138.80 | Premium contraction to the 50% target |
| 734/724 x1, 2026-07-28 | Opened 2026-07-20 at 1.59 | TP at 0.79 | +$75.40 | Premium contraction to the 50% target |
| 735/725 x1, 2026-07-28 | Opened 2026-07-20 at 1.64 | TP at 0.82 | +$79.40 | Premium contraction to the 50% target |

Total: $293.60, matching `realized_today` and the July 21 history bucket.

### Open-position risk at EOD

- Bot B 740/730: mark 1.50 versus stop 5.13; short 1.13 ATR below spot; +$21 unrealized.
- Bot B 741/731: mark 1.62 versus stop 4.83; short 1.00 ATR below spot; -$1 unrealized.
- Bot C 742/741: mark 0.18 versus stop 0.51; short 0.86 ATR below spot; -$1 synthetic unrealized.
- Bot C 739/734: mark 0.49 versus stop 2.76; short 1.27 ATR below spot; +$43 synthetic unrealized.
- Threat rule: approximately 0.25 ATR to the short strike or mark near the 3x stop. None qualified.

## Material findings

### Finding 1 — Tuesday's realized gain came from Monday-origin positions, not Tuesday entries

- Causal classification: **strategy flaw** — the material issue is schedule attribution: treating today's positive total as evidence for all-days entry would confound Monday carry with Tuesday entries.
- Evidence:
  - All three July 21 closes were opened July 20 and produced $293.60.
  - The two July 21 entries contributed only $20 of unrealized sandbox P&L by EOD.
  - Existing one-year research reports Monday-only PF 1.86 and 1.39 under 2x costs, versus all-days PF 1.03 and 0.76 under 2x costs.
- Confidence: **High**.
- Known limitations: The Monday-only implementation may not have selected and sized the July 20 positions identically; Bot B actual fills are sandbox-unaudited.
- Epistemic status: **Inferred** from observed entry dates plus previously completed replay evidence.

### Finding 2 — Bot C's zero-entry day was caused by configured risk capacity, not an operational halt

- Causal classification: **configuration problem** — the small live account and existing book met the configured sizing limits; this describes capacity, not a recommendation to loosen limits.
- Evidence:
  - `halted=false`, no pending orders, and no quote/order-error decision categories today.
  - 277 decisions were blocked by `total_stop`; 67 were zeroed at `requested_qty`.
  - Equity was $891.03 and the configured total-stop cap was $267.31.
- Confidence: **Moderate**.
- Known limitations: The exported decision rows omit the active binding-cap exposure, limit, incremental risk, and final quantity.
- Epistemic status: **Observed** for gate counts; **Inferred** for the account-capacity interpretation.

### Finding 3 — Broker reconciliation is clean; Bot B's extra spread is a shared-account co-occupant

- Causal classification: **operational interference**.
- Evidence:
  - Bot B owned legs exactly reproduce 740/730 x1 and 741/731 x1.
  - The 736 short and 726 long are tagged `foreign`; the snapshot states Bot B runs `--shared-account`.
  - Bot C's four owned legs exactly reproduce 742/741 x1 and 739/734 x1.
  - Both performance equities equal their broker-snapshot equities; both pending-order arrays are empty.
- Confidence: **Very High**.
- Known limitations: Reconciliation uses a synchronized point-in-time snapshot, not a broker statement or independently observed fills.
- Epistemic status: **Observed**.

### Finding 4 — The standardized decision export omits the active quantity-cap telemetry

- Causal classification: **code defect**.
- Evidence:
  - All 743 decision rows across both bots have blank `risk_budget_proposed_qty`, `risk_budget_permitted_qty`, and `risk_budget_headroom`.
  - The deployed orchestrator populates `requested_qty`, `final_qty`, `risk_current_exposure`, `risk_limit`, `risk_remaining_capacity`, and `risk_incremental_per_contract`.
  - `reports/gen_report.py` exports the legacy blank `risk_budget_*` names instead of those active fields.
  - The 43 focused report tests and the full bot test suite passed, so existing tests do not catch this end-to-end telemetry mismatch.
- Confidence: **Very High**.
- Known limitations: The dry run did not read production logs or patch the exporter, per instruction.
- Epistemic status: **Observed**.

### Finding 5 — Reported P&L has two different accounting standards

- Causal classification: **accounting problem**.
- Evidence:
  - Bot B declares `actual_fill / sandbox_unaudited` and reports $293.60 realized.
  - Bot C declares `synthetic_mark / live_fill_accounting_disabled`, reports zero realized today, and $42 synthetic unrealized.
  - Bot B history before July 20 carries the documented fill-sign/quantity distortion.
- Confidence: **Very High**.
- Known limitations: No broker statements or audited fill ledger were available; cross-bot dollar comparisons are not like-for-like.
- Epistemic status: **Observed**.

### Finding 6 — The market path was favorable to existing short-put spreads without producing a full expected move

- Causal classification: **market loss** — taxonomy label for market-driven P&L causality; the observed effect today was favorable rather than a loss.
- Evidence:
  - SPY rose 1.99 points, or 0.267%, from open to daily close.
  - The 4.86-point range was 0.67 ATR and 60.4% of the 8.04-point expected move.
  - SPY broke above the opening range and finished above the validated 747.57 session VWAP.
  - Three Monday-origin spreads reached their 50% take-profit targets; no open short was within 0.25 ATR of spot.
- Confidence: **Moderate**.
- Known limitations: Causality also includes theta and IV changes; per-position IV is missing, and Bot C marks are synthetic.
- Epistemic status: **Inferred** from market path and spread outcomes.

### Finding 7 — Bot B's rejected candidates looked favorable at 60 minutes, but that is not evidence to loosen the gap cap

- Causal classification: **strategy flaw** — possible opportunity cost from the entry/risk-selection design, not a demonstrated configuration error.
- Evidence:
  - 25 of 37 Bot B rejected candidates were favorable to the seller at 60 minutes (67.6%).
  - Mean 60-minute spread move was -0.0792.
  - Bot C's corresponding figures were 13 of 36 (36.1%) and -0.0111.
- Confidence: **Low**.
- Known limitations: The window ends at 60 minutes, candidate strikes and timestamps are absent, outcomes are not full-hold P&L, and repeated evaluations may not be independent.
- Epistemic status: **Observed** for mark-outs; **Speculative** for persistent opportunity cost.

## Strategy Scientist counterfactuals

- **Monday-only:** It was Tuesday, so it would not initiate today's 740/730 and 741/731 spreads. It could still have managed Monday-origin positions and plausibly captured today's three TPs. Status: **Inferred**; Confidence: **Moderate**; limitation: exact Monday-only selection/sizing is not reconstructed.
- **0.25 vs 0.35 delta:** Not answerable from the synchronized snapshot. Today's entries were 0.2758 and 0.2943 delta, but no contemporaneous alternate-strike chain or forward mark is retained. Status: **Speculative**; Confidence: **Speculative**.
- **Deeper strike:** Not answerable. The decision export lacks rejected-candidate strikes and incremental 2-ATR risk. Status: **Speculative**; Confidence: **Speculative**.
- **Later entry:** Not answerable because normalized decisions and trades contain dates but no timestamps. Status: **Speculative**; Confidence: **Speculative**.
- **Shorter DTE:** Not answerable without same-time alternate-expiration chains and forward marks. Status: **Speculative**; Confidence: **Speculative**.
- **Different exits:** Today's three 50% TPs succeeded. EOD marks for the closed spreads are not retained, so EOD-close, trailing, and wider/narrower TP counterfactuals cannot be priced. Existing local replay evidence warns that early-loss exits caused substantial whipsaw. Status: **Inferred**; Confidence: **Low**.

## Engineering review and tests

- The working tree is nine commits ahead of the deployed commit. Trading-code differences include a startup guard that pins credit floors; other changes primarily add review infrastructure, security cleanup, and tests.
- No trading-code change or patch branch was made in this dry run.
- Focused report tests: 43 passed.
- Full `bot/tests` suite: exit 0, 100% passed.
- Recommended report-only follow-up: export and test the active quantity-cap telemetry fields. This is recorded as Pending in advisor memory.

## Overall confidence and limitations

Overall confidence: **High** for reconciliation, trade accounting within each declared provenance, gate counts, and the report-layer telemetry defect; **Low to Moderate** for strategy counterfactuals based on a single day.

Primary limitations: one-day sample, Bot B sandbox fills unaudited, Bot C synthetic P&L, pre-July-20 Bot B distortion, 60-minute-only rejected-candidate mark-outs, no decision timestamps/strikes, no alternate-chain archive, and no external broker statement.
