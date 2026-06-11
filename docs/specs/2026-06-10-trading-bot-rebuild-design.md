# Trading-Bot Rebuild — Option C Staged Portfolio (Design Spec)

Date: 2026-06-10
Status: DRAFT — pending Shawn's review
Decisions locked: capital $5k–$25k fresh · staged portfolio (Option C) · all legacy bots stopped 2026-06-10 · futures Stage-3 route = prop-firm eval · UW subscription retained pending empirical edge study (running)

Companion documents:
- Diagnosis: `reports/2026-06-10-bot-fleet-diagnosis.md` (why the old fleet lost; verified edges to reuse)
- UW edge study: `research/2026-06-10-uw-flow-edge-study.md` (in progress)

---

## 1. Goal and honest framing

Build a small portfolio of two independent strategies on one shared, truth-first core, prove or disprove their edge on paper within ~8 weeks under predefined PASS/ABANDON gates, and deploy live only what passes.

**Earnings framing (explicit):** $4–5k/month is the aspiration, not a design promise. At $15k deployed and a proven 2–4%/month edge, expect $300–600/month from personal capital. The bridge toward $4–5k/month is Stage 4: prop-firm funded accounts (futures leg) and/or capital scaling once a verified track record exists. Any design claiming $4–5k/month directly from ≤$25k would require 18–90% monthly returns and is rejected as fantasy.

**Non-goals:** crypto trading; 0DTE strategies; ML-driven signals (the old ML state trained on corrupted records); reusing thales (audited: stub, never traded); resurrecting any legacy bot as-is.

---

## 2. Why the old fleet failed (design constraints derived from evidence)

Each failure becomes a hard constraint on this design:

| Evidence (see diagnosis) | Design constraint |
|---|---|
| March +$607 vs April −$1,856, same strategy; ~94% of April losses = regime mismatch | **C1.** Every direction-sensitive entry passes a regime gate. No exceptions. |
| Bot logs drifted +$69k from broker truth; 80% order-reject storms unnoticed; drift ALARM −$684/30d | **C2.** Broker fills are the only P&L record. Fill verification is fail-closed. Reconcile daily; drift > $50 pages. |
| Phase E EV gate sealed the funnel; bot silent 5 weeks while "healthy" | **C3.** Dead-man monitoring on outcomes (trades, fills, feeds), not process liveness. Filter funnels must be observable (per-gate rejection counters, alert on 100% rejection over 3 sessions). |
| In-sample PF 5–7 → OOS PF 0.5–0.8, every sweep; gates validated on n=14 | **C4.** No parameter goes live without out-of-sample consistency at n≥60. Parameter plateaus over point-optima. |
| Box/Dow + PATH B deployed despite failing their own gates; stacked changes | **C5.** Predefined PASS/ABANDON criteria with decision dates, written before the experiment starts. One change at a time. |
| Daytrader frozen by expired phantom positions; QCOM order timed out unfilled silently | **C6.** Position lifecycle is state-machine-managed: every position has an expiry-cleanup path and every order an unfilled-timeout path. |
| $100 sizing vs $200–330 spread max-loss; fee drag at 1-contract scale | **C7.** Strategy/account fit is checked at config load: if p95 structure max-loss > per-trade risk budget, refuse to start. |
| Live API key hardcoded in source, overriding .env | **C8.** Credentials only from environment/config; startup banner states account type (live/sandbox) + account id suffix; live mode requires an explicit `I_UNDERSTAND_THIS_IS_LIVE=yes` env. |

---

## 3. Architecture

```
fable-options/                      (this repo; deploys to droplet 159.89.45.162)
├── core/
│   ├── truth_ledger.py        ← broker-truth accounting (Tradier + IBKR adapters)
│   ├── regime.py              ← daily regime state (trend / sector-rotation / IV)
│   ├── risk_gate.py           ← single pre-trade chokepoint: is_order_allowed()
│   ├── monitor.py             ← dead-man + drift + funnel alerts (Telegram/email)
│   ├── broker/
│   │   ├── tradier.py         ← REST adapter, fill verification, order state machine
│   │   └── ibkr.py            ← ib_insync adapter (reuse legacy gateway/IBC stack)
│   └── registry.py            ← strategy plug-in contract: init() → on_bar/on_scan() → [Signal]
├── strategies/
│   ├── credit_spreads.py      ← options leg (Stage 1)
│   └── mes_trend.py           ← futures leg (Stage 2)
├── config/                    ← one JSON per strategy instance; schema-validated
├── ops/                       ← deploy scripts, systemd units, runbooks
└── journal/                   ← experiment log: every config change, dated, with reason
```

### 3.1 Truth ledger (C2)
- Pulls Tradier `history`/`gainloss` and IBKR fills daily (reuse and harden the legacy `phase_a0a/a0b` reconstruction approach — it was the project's best engineering).
- Every order submission must parse a broker `order.id` or the order is treated as failed and retried/abandoned explicitly (C6). Unfilled limit orders cancel after a configured timeout and the position state machine records the terminal state.
- Daily reconcile bot-state vs broker; |drift| > $50 → page + halt new entries until acknowledged.
- Single SQLite DB (`journal.db`) for orders, fills, positions, daily equity — replacing the legacy CSV sprawl.

### 3.2 Regime service (C1)
Computed once daily after close, consumed by both strategies:
- **Trend:** SPY close vs 20d/50d SMA stack → UP / DOWN / CHOP.
- **Rotation:** cyclical (XLY,XLK,XLI,XLF) vs defensive (XLP,XLU,XLV) 20d relative strength → CYCLICAL_LED / DEFENSIVE_LED. (Legacy finding: BCS in cyclical-led tape PF 0.05 vs 1.36 otherwise.)
- **Vol:** VIX level + each underlying's IV rank from Tradier chains.
Regime history is persisted so backtests use point-in-time values (no lookahead).

### 3.3 Risk gate (C7, C8)
One function in one module through which every order passes:
- per-trade risk ≤ 5% of current broker-truth equity;
- total open risk ≤ 25% of equity; max 3 concurrent positions per strategy;
- daily realized loss ≥ 2% of equity → strategy halts until next session;
- same-ticker 5-session cooldown after a loss (legacy COIN lesson);
- duplicate-close guard (legacy XOM phantom-close lesson);
- prop-eval mode: trailing-drawdown and daily-loss limits enforced bot-side at 80% of the firm's published limits (built now, per the prop-eval decision).

### 3.4 Monitoring (C3)
- Outcome heartbeats: orders attempted/filled per session, per-gate rejection counters, feed staleness, positions nearing expiry.
- Pages (Telegram bot or email) on: zero entries for 3 sessions while signals fired; any position within 1 day of expiry not flagged for close; order-reject streak ≥ 5; reconcile drift; process down > 10 min during market hours.
- Weekly auto-summary: broker-truth P&L, win rate, payoff, funnel stats — delivered, not buried in logs.

---

## 4. Strategy legs

### 4.1 Stage 1 — `credit_spreads` (Tradier, paper first)
Defined-risk verticals on a liquid whitelist (SPY, QQQ, IWM + ~10 mega-caps with penny-wide weeklies).
- **Bull put spread** only when: regime ≠ DOWN, **and** underlying IV rank ≥ 35% (legacy: 79% vs 46% WR), and short strike ≤ 30-delta, DTE 7–21.
- **Bear call spread** only when: regime = DOWN **and** DEFENSIVE_LED (legacy: tactical tool, not daily default).
- **Flow corroboration (pending UW study):** if the edge study validates specific slices, matching UW flow within the session adds conviction/size; it never originates a trade alone. If the study finds nothing robust, UW is dropped and the subscription cancelled.
- Credit ≥ 25% of width. **Stop-loss enabled:** close at loss = 1.0× credit received (kills the 1:6.5 realized reward/risk of the legacy bot). TP at 50% of credit. Time exit at 2 DTE, no exceptions, no rolling (legacy rolling masked losers).
- Geometry sanity: at 30-delta/~70% win odds with 1.0× credit stop and 0.5× credit target, expectancy is positive at ≥67% realized WR; the regime+IV filters exist precisely to buy those extra points over the unconditional ~55–65%. This claim is validated in backtest before paper (C4).

### 4.2 Stage 2 — `mes_trend` (IBKR paper via legacy gateway stack)
MES, 5-min bars, long-biased, regime-gated (no shorts while regime = UP — legacy ORB shorts lost −$431 while longs made +$45):
- Setups: (a) opening-range breakout **long-only in UP regime** with the chop-day damage cap; (b) `midpoint_reclaim` family (the only legacy-positive setup), both directions but regime-aligned.
- Brackets sized for realized slippage: stops assume ~1.5 pt average adverse fill (legacy: −$78 realized vs −$60 design); EOD flatten at 15:45 ET to stop winner-truncation at the close.
- Risk: 1 contract; bot-side daily loss cap $150; built against prop-eval trailing-drawdown semantics from day 1.

---

## 5. Stages and gates (C5)

| Stage | What | Duration | PASS gate (predefined) | On FAIL |
|---|---|---|---|---|
| 0 | Shared core + both strategies in backtest | ~2 weeks build | Backtest: n≥60 OOS trades/strategy, PF ≥ 1.3 OOS, parameter plateau shown | Redesign strategy leg; core is kept regardless |
| 1+2 | Both legs paper, concurrently (Tradier sandbox + IBKR paper) | 6–8 weeks or 60 trades/leg, whichever later | PF ≥ 1.3, realized payoff within 20% of design, zero unexplained drift, zero dead-man incidents | One revision round max (single change), else ABANDON leg |
| 3 | Options leg → live small ($5–10k Tradier, 1–2% risk/trade); futures leg → prop eval #1 | rolling monthly review | Live monthly PF ≥ 1.2 over ≥20 trades; eval: pass without bot-side limit breaches | Drop to paper, post-mortem, one revision, else kill |
| 4 | Scale: add eval accounts / raise options sizing toward 5% | quarterly | Sustained live record | Hold or shrink |

Experiment journal (`journal/`) records every config change with date + reason; the config that runs is never edited mid-experiment.

**Decision date for the whole program:** if by 2026-09-15 neither leg has passed Stage 1/2, stop and reassess the entire venture rather than iterating forever.

---

## 6. Testing

- Core modules (ledger, risk gate, regime, order state machine): unit-tested, TDD; broker adapters tested against recorded HTTP fixtures + sandbox.
- Backtests: walk-forward with time-split OOS; costs modeled (Tradier $0.35/contract+fees ≈ $1.30/spread round-trip; MES slippage 1.5 pt); regime values point-in-time.
- Paper = production code against sandbox/paper endpoints — same binary, different config (legacy paper/live divergence is banned).
- Chaos drills before live: kill the process mid-position, expire a position in state, force an order timeout — each must alert and recover per the state machine.

## 7. Deployment & ops

- Same droplet (159.89.45.162), but as **systemd services** (auto-restart, journald) instead of screen sessions; legacy dirs left untouched/archived.
- IB Gateway: reuse the legacy IBC/Docker stack with the nightly-restart reconnect bug fixed (bot reconnects with backoff instead of exiting).
- Secrets in `/etc/fable-options/env` (root-only), never in source (C8). Live Tradier key rotated by Shawn before Stage 3.
- Costs while paper: $0 new (droplet exists; UW kept pending study; Tradier sandbox + IBKR paper free). Stage 3 adds one prop eval (~$50–170/mo).

## 8. Open items

1. UW flow edge study (running) → determines the flow-corroboration component and the subscription decision.
2. Prop firm selection (Topstep vs Apex vs Tradeify et al.) — research due before Stage 3, criteria: MES allowed, automation policy, trailing-DD type, payout terms.
3. Telegram vs email for paging — default Telegram bot unless Shawn objects.
