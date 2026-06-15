# S2b Execution Bot — Design Spec

Date: 2026-06-15 · Status: design, pending review → implementation plan.
Builds on: `docs/specs/2026-06-10-trading-bot-rebuild-design.md` (core architecture, C1–C8) and the 2026-06-10→15 research arc. This spec is the **strategy + risk-control + sizing layer**; it reuses the rebuild spec's truth-ledger / risk-gate / order-state-machine / monitoring core rather than redefining it.

## 1. Objective

Turn the one validated, OOS-robust, scalable edge — **S2b** (Monday SPY defined-risk put spread, PF 1.86–1.97 across both halves) — into a live, risk-controlled, scalable income engine. Every design choice below is backed by a measured result from this project, not intuition.

## 2. What the research settled (the evidence this spec encodes)

| Decision | Evidence |
|---|---|
| Edge = mechanical index VRP (S2b), **not** flow/prediction | flow direction disproven 7× (daily, intraday, large-whale, bot-B live logs); ML multifactor AUC 0.47; entry-rule search permutation-null p=0.18 |
| **Monday-primary**, not all-days | SPY-Mon PF 1.89 / H2 1.97; Wed 1.08, Fri 1.42, MWF 1.29 — all dilute and fail 2× stress |
| **Scale via SIZE, not days** | PF ~size-invariant: 1.89 @10% → 1.73 @20% → 1.63 @40%; income $310→$1,792/mo; drawdown 4%→23% |
| **Working stops are the missing fix** | bot B set `scaled_stop_level` on **0/114** trades; losers ran to −80/−378% at 1 DTE; March +$772 → −$1,405 was the unhedged reversal |
| **≥1 ATR cushion** on short strikes | live winners avg 0.73 ATR vs losers 0.38 ATR cushion; only 10/81 had ≥1 ATR |
| **Regime-aware sizing** (cut when VIX spiking) | March run-up rode VIX +23.6% (max reward *and* max reversal risk); the reversal cost 3× the run |
| **No directional debit spreads** | bot-B bull-calls went 1-for-6 |
| **Index-only, defined-risk, cash account** | single-name has fat idiosyncratic tails + wide spreads; index is deep/scalable; cash account avoids PDT |

## 3. Strategy (frozen S2b parameters — do not re-optimize without a new pre-registration)

- **Underlying:** SPY (primary). Optional small secondary sleeve: a *lower-conviction* Friday SPY entry at reduced size (Friday has weak edge, PF 1.42 but fails 2× stress — so it runs at ≤½ Monday size and is the first thing cut under stress). **Monday carries the book.**
- **Entry:** Monday 10:00 ET, SPY **bull put spread**, short ~**30–40Δ** put, **$5–10 wing**, nearest weekly expiry **≥4 DTE** (Friday).
- **Cushion gate:** reject the entry if the short strike is **< 1 ATR(14) OTM** (new — the strike-discipline fix).
- **Management:** **TP 50%** of credit · **stop 2.0× credit (hard, must fire)** · **time-exit at 1 DTE**. (Exit sweep confirmed 50%/2.0× is OOS-optimal; tighter stops backfire via whipsaw.)
- **Structures allowed:** defined-risk credit spreads only. **Directional debit spreads are forbidden.**

## 4. The critical fix — stops that actually fire

Bot B's stops existed in config but never executed. This is the single highest-value change. Requirements:
1. **Continuous mark monitoring** of every open spread against its stop level on each polling cycle (not just at entry/expiry).
2. When mark ≥ stop level → submit closing order through the order state machine; **verify the fill** (parse broker `order.id`; cancel-and-retry on timeout; never assume closed — bot B's "5 failed close attempts" lesson).
3. **Dead-man check:** if a position is past its stop and not closed within N cycles → page + halt new entries (C2/C3).
4. Stops are enforced **bot-side**, independent of any broker-side order, so a missed broker stop still triggers.

## 5. Sizing & scaling policy

- **Base risk/trade:** start **10% of current equity** vs spread max-loss (validated; $310/mo on $20k at PF 1.89).
- **Scaling frontier (PF-preserving):** size may scale toward **15–25%** for more income (PF stays ~1.7–1.8; drawdown 9–14%). Hard ceiling **25%** until a live track record justifies more — beyond that PF erodes and drawdowns exceed 15%.
- **Regime-aware size cut (new):** when **VIX is elevated/spiking** (e.g., VIX > its 80th percentile or 1-day VIX change > +15%), **halve** position size. This directly addresses the March/April lesson — the reversal hurts most exactly when vol is high.
- **Cash-account settled-funds logic:** track settled vs unsettled cash; never open a position funded by unsettled proceeds (avoids good-faith violations → 90-day lockout). Naturally throttles to a few new positions/day, which matches the Monday-primary cadence.

## 6. Account & regulatory

- **Cash account, $5–15k**, options level 3 (defined-risk only — already approved). No PDT (cash account). Settled-funds discipline per §5.
- Live credentials from environment only (C8); startup banner states account type + id suffix; live mode requires explicit `I_UNDERSTAND_THIS_IS_LIVE=yes`.

## 7. Architecture (reuse the rebuild-spec core)

Per `2026-06-10-trading-bot-rebuild-design.md`: `truth_ledger` (broker-truth accounting, daily reconcile, drift > $50 → halt), `risk_gate` (single pre-trade chokepoint: per-trade ≤ sizing cap, total open risk cap, max concurrent, daily-loss halt, same-ticker cooldown, duplicate-close guard), `broker/tradier` (REST adapter, fill verification, order state machine), `monitor` (dead-man on outcomes + funnel + drift, Telegram/email). The S2b strategy plugs into the `registry` strategy contract. **Paper = same binary as live, different config** (no paper/live divergence).

## 8. Deployment gates (predefined PASS/ABANDON, C5)

| Phase | Action | PASS to advance | Else |
|---|---|---|---|
| 0 | Paper-trade S2b managed (same binary, sandbox) ≥ 6 weeks / ≥ 20 Mondays | Live monthly PF ≥ 1.3 over ≥ 20 trades; **stops verified firing in ≥ 1 chaos drill** | Fix, one revision, re-run |
| 1 | Live small ($5–10k, 10% sizing) | PF ≥ 1.2 over ≥ 20 live trades, no drift > $50, no stop-failure | Drop to paper, post-mortem |
| 2 | Scale sizing toward 20–25% | Sustained live PF ≥ 1.4, drawdown within tolerance | Hold or shrink |

**Chaos drills before live (C-required):** kill the process mid-position; force a stop to trigger; force an order timeout — each must alert and recover. **A stop that does not demonstrably fire in a drill blocks live deployment.**

## 9. Explicitly excluded (with evidence — do not revisit without new pre-registration)

- Options-flow direction / large-whale entry (disproven 7×) · multi-factor ML/rule entry gating (AUC 0.47; permutation-null p=0.18) · IV-rank conditioning (failed 3×) · all-days mechanical entries (dilute PF, fail stress) · single-name underlyings (idiosyncratic tail, not scalable) · naked/undefined-risk structures · directional debit spreads (1-for-6 live).

## 10. Success metric

Not "PF 2.0" (shown to be unreachable net/OOS/stress here, and a target that incentivizes hidden tail risk). The metric is **sustained live PF ≥ 1.4 net at 2× modeled costs, with drawdown ≤ 15% of equity, scaled to the largest size that keeps both** — i.e. maximize *dollars* on the robust ~1.8 edge, not the ratio.

## 11. Open items for the implementation plan

- Polling cadence / data source for live marks (Tradier quotes) and the stop-monitor loop.
- ATR(14) and VIX-percentile feeds (point-in-time) for the cushion gate and regime sizing.
- Exact settled-funds accounting against Tradier cash balances.
- Friday secondary-sleeve on/off default (recommend **off** at launch; add only after Monday is proven live).
