# Proposed-Rules Credit-Spread Replay Validation — 2026-06-10/11

**Question:** does the proposed "fable-options credit_spreads leg" rule set (design spec §4.1)
show OOS-consistent positive expectancy when replayed against the project's historical
flow-alert + options-bar archive?

**Verdict: FAIL.** The spec configuration loses decisively (full-year PF 0.52, hold-out-half
PF 0.64), and all three improved candidates surfaced by the sweep/ablations collapse on the
untouched second half (PF 1.35–1.84 in-sample → 0.60–0.79 out-of-sample). No tested
configuration shows out-of-sample positive expectancy on this data. Details and what this
does/doesn't mean in §8.

---

## 1. Data audit

### 1.1 Alert / flow data the engine consumes

| source | schema | date range | sessions | notes |
|---|---|---|---|---|
| droplet `/root/trading-bot/data/whalestream_options/` (260 daily JSONs, 3.4GB) | whalestream alert JSON (incl. per-alert delta, IV, NBBO bid/ask) | **2025-03-03 → 2026-02-27** | **250** | THE year-long archive; matches the option-bar cache. Converted locally to engine CSV (1,267,731 alerts after premium ≥ $50K + watchlist filter — both filters are engine-equivalent). |
| local `E:\...\backtests\uw_flow_history_live_b.csv` (226MB) | UW history CSV | 2026-03-09 → 2026-04-08 | 23 | Too short; barely any option-bar cache backing (3.3K keys vs 30K+/month for 2025). |
| droplet `/root/trading-bot/data/uw_flow_history_live_b.csv` (845MB) | UW history CSV | 2026-03-09 → 2026-06-10 | ~63 | Newer live window; essentially uncached for replay (would need full Polygon re-pull). |
| local `E:\...\uw_flow_history.csv` (24MB) | UW history CSV | 2026-03-05/06 | 2 | Negligible. |

### 1.2 Option-bar coverage in `api_cache.db` (1.5GB, 446,115 rows)

- 440,783 `polygon:bars_*` keys (5-min option bars), spanning **2025-03 → 2026-02 densely**
  (26K–48K keys/month), tapering to nearly nothing in 2026-03+.
- Ticker concentration: QQQ 107K, SPY 99K, MSFT 39K, META 39K, AAPL 38K, NVDA 35K,
  TSLA 33K, IWM 23K; everything else ≤ 1.6K keys.
- DTE-at-query distribution: dense 0–21 days, thin beyond 25.
- 4,580 `tradier:strikes_*` keys, 243 VIX daily closes, **0 iVolatility keys**.

### 1.3 Does "1 year of options backtest data" exist?

**Yes — 2025-03-03 → 2026-02-27 (250 sessions)**, via the whalestream JSON archive (flow side)
+ the Polygon bar cache (pricing side). The newer 2026 UW-format flow files have essentially
no cached bars. All runs below use this window. Halves: H1 = 2025-03-03 → 2025-08-28,
H2 = 2025-08-29 → 2026-02-27.

### 1.4 Execution-model reality check

- The Polygon/Massive key in the legacy source works (tested live; ~2,500 fresh pulls made).
- The iVolatility key returns 403 and the cache holds zero iVol entries — the legacy engine's
  advertised "CALIBRATED SLIPPAGE" mode has in practice always fallen back to fixed $0.05/leg.
  **Every historical "calibrated" backtest in the archive was optimistic on fills.**
- These runs instead use an empirical NBBO half-spread model built from the whalestream alerts
  (per-symbol median (ask−bid)/2 on OTM contracts, |delta| 0.05–0.40, DTE 4–25, floored at
  $0.05/leg): $0.05 for AAPL/AMZN/GOOGL/IWM/MSFT/NVDA/QQQ/SPY/TSLA, $0.075 for META.
- Commissions: $1.30 per spread round trip.

## 2. Implementation notes & deviations from spec

- **Entry-opportunity stream is flow-alert-conditioned.** The archive only prices spreads where
  whale alerts pointed; the simulator therefore tests the spec's *filters/exits* on
  flow-originated candidates, not the spec's chain-scan origination. See limitations.
- Regime series built point-in-time from daily closes (SPY 20d/50d SMA stack;
  cyclical XLY/XLK/XLI/XLF vs defensive XLP/XLU/XLV 20d relative strength); persisted to
  `regime_series.csv`; no lookahead (prior close only).
- Short-strike delta computed via Black-Scholes (`bs_delta`) from spot, strike, DTE, and
  per-symbol daily median alert IV — a proxy, since the archive lacks per-strike Greeks at
  scan time.
- IV rank = percentile of the symbol's daily median alert IV vs its trailing 60 sessions
  (`build_iv_daily`/`iv_rank`); sanity-checked at 0.773 correlation vs cached VIX closes.
- Portfolio rules per spec: $10k account, 5% risk/trade, max 3 concurrent, 2% daily-loss halt,
  5-session same-ticker cooldown after a loss. Exits: TP 50% of credit, stop at 1.0× credit,
  time exit 2 DTE, no rolling (varied in sweep/ablations as labeled).

## 3. Results — spec configuration and ablations (full window unless noted)

| run | window | n | WR | PF | P&L ($10k acct) | max DD |
|---|---|---|---|---|---|---|
| **proposed_full (spec §4.1)** | full year | 48 | 54% | **0.52** | **−$2,461** | $3,052 |
| **oos_half2 (spec, locked)** | H2 only | 26 | 54% | **0.64** | **−$1,207** | $2,168 |
| abl_no_regime (regime gate off) | full | 55 | 58% | 0.60 | −$2,132 | $2,636 |
| abl_no_iv (IV-rank filter off) | full | 65 | 62% | **1.13** | **+$602** | $1,511 |
| abl_no_stop (stop disabled) | full | 43 | 60% | 0.49 | −$2,459 | $2,904 |

Ablation reading: the regime gate is ~neutral on this data; the 1.0× stop is
indistinguishable from no stop (0.52 vs 0.49); the IV≥35% filter is the single largest
*drag* — it concentrated entries into the spring-2025 crash and silenced the strategy
through the calm H2 rally. (The legacy "IV≥35% → 79% WR" finding came from a different,
2026-only window — a regime truth, not a law.)

## 4. Plateau analysis (27 configs, H1 only)

Stop {0.75, 1.0, 1.5}× credit × TP {40, 50, 60}% × short-delta cap {0.25, 0.30, 0.35}:

- **20 of 27 configs have PF < 1.0.** The spec point (1.0×/50%/0.30) sits in a uniformly
  losing neighborhood (PF 0.72).
- Only coherent positive region: **stop 1.5× with delta cap 0.35** — PF 1.20/1.35/1.34
  across all three TP levels (n=29–31, WR ~76%). Tight stops get whipsawed by
  mark-to-market noise before short-DTE spreads can resolve.
- n per cell is 13–36 — far below the n≥60 validation gate; the sweep can suggest
  geometry, not validate it.

## 5. Half-1 vs half-2 consistency (hold-out tests)

Candidates were selected by looking at H1/full-window results (post-hoc); H2 was their first
contact with unseen data:

| candidate | H1 | H2 (hold-out) | verdict |
|---|---|---|---|
| A: stop 1.5×, TP 50%, delta 0.35 | PF 1.35, +$548 (n=30) | **PF 0.60, −$2,462 (n=54)** | FAIL |
| B: spec minus IV filter | PF 1.71, +$1,152 (n=34) | **PF 0.79, −$610 (n=30)** | FAIL |
| C: A + B combined | PF 1.84, +$2,025 (n=56) | **PF 0.74, −$1,538 (n=59)** | FAIL |

All three "edges" lived in H1 (crash + recovery, high vol) and died in H2 (grind-up, low vol).
This is the exact in-sample→OOS collapse signature that destroyed the legacy project,
reproduced here under controlled conditions before any deployment.

## 6. Legacy baseline anchor

The legacy-default baseline run (engine defaults, whale-conviction entries, x10 contracts)
crashed twice (unbounded in-memory bar cache → silent death ~6 weeks in; API rate-limit
contention). Its partial trace (Mar–mid-Apr 2025) showed the familiar legacy optimism
(+$9k on $25k in 5 weeks, fixed $0.05 slippage, TP fills at bar prices). It is not material
to the verdict and was deprioritized; can be rerun overnight for completeness if wanted.

## 7. Limitations

1. **Alert-conditioned universe**: entries could only originate where whale alerts pointed.
   The companion UW edge study independently found that stream carries no directional edge —
   so this replay tests "spec filters on edgeless entries." It cannot test the spec's true
   chain-scan origination (no historical chain snapshots exist). It CAN and did test exit
   geometry, gates, and the overall flow-conditioned approach.
2. Delta and IV-rank are proxies built from alert IVs (documented above).
3. Fill model: bar-close ± empirical half-spread; no queue/partial-fill modeling. Optimistic
   if anything — which makes the losing results more, not less, damning.
4. One year, one market arc (crash → recovery → grind-up). Two halves ≈ two regimes; that is
   the point of the split, but n per regime is modest.
5. Multiple comparisons: 37 runs total; the hold-out gate is the only protection, and nothing
   survived it.

## 8. Verdict

**FAIL — do not build the options credit-spread leg as specced (§4.1), and do not "fix" it by
adopting any of the sweep winners; they failed hold-out.**

What this does and does not mean:

- It **invalidates**: flow-alert-conditioned credit-spread entries under every exit/gate
  combination tested (37 runs), on the only year of data that exists. Combined with the UW
  edge study (no directional edge in the alert stream itself, in-sample or out), the entire
  flow→credit-spread pipeline — the heart of livebot a/b/c/k — is now double-condemned by
  evidence.
- It does **not** invalidate: (a) the shared core (truth ledger / regime service / risk gate /
  monitoring) — edge-agnostic infrastructure, still correct to build; (b) the futures leg —
  untested by this replay, has its own data (databento MES prefetches) for separate
  validation; (c) chain-scan-originated options strategies — structurally untestable on this
  archive; validating those requires capturing daily chain snapshots going forward (start
  now, validate in 2–3 months on real captured data).

**Recommended path:** lead with futures-leg validation next (data exists today); build the
shared core in parallel; start chain-snapshot capture immediately so a future options leg can
be validated honestly; spend nothing further on flow-driven options entries.
