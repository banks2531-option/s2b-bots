# Mechanical-Strategy Replay Harness — design notes + smoke results (2026-06-11)

New files (nothing existing modified): `underlying_bars.py`, `sim_mech.py`
(+ derived artifacts: `prefetch_underlying.log`, `mech_iv_slip_cache.json`,
`trades_mech_smoke_202506_*.csv`, `summary_mech_smoke_202506.json`).

Purpose: decouple entry origination from the whalestream flow-alert stream
(the structural limitation of `sim_proposed.py`) so mechanical strategies —
entries defined purely by clock/underlying-price rules — can be replayed
against the same year of 5-min option-bar data (2025-03-03 → 2026-02-27).

## 1. underlying_bars.py — 5-min stock bars

- Source: Polygon/Massive stock aggregates (`/v2/aggs/ticker/{SYM}/range/5/minute/...`),
  same API key + base URL as `engine_v345.PolygonClient`, paced at 0.30s.
- Cache: same two-tier disk cache (E:\ archive read-only + local overlay),
  one key per (symbol, ET session date): `polygon:ubars_{SYM}_{date}_5_minute`,
  value = list of `{t,o,h,l,c,v}` (raw unix-ms UTC `t`; `date_et`/`time_et`
  convenience fields attached on read, not stored).
- **Prefetch optimization**: instead of ~500 per-day calls, one ranged request
  per (symbol, calendar month) split into per-day keys → **24 fresh API calls**
  covered all 250 sessions × {SPY, QQQ} (log: `prefetch_underlying.log`).
  Sessions with no bars are stored as `[]` so re-lookups never re-hit the API.
- ET conversion uses `zoneinfo` (real DST). NOTE: the engine's `ts_to_date`/
  `ts_to_time` assume fixed UTC-5 and are **one hour wrong Mar–Nov**; all time
  logic in the new harness therefore works from unix-ms + zoneinfo, never the
  engine's converters.

## 2. sim_mech.py — Strategy interface + portfolio harness

### Interface

```python
class MechStrategy:
    symbols = ['SPY']                      # underlyings that drive on_bar
    def on_session_start(self, date, ctx)
    def on_bar(self, dt, bars_so_far, ctx) -> list[SpreadOrder | None]
```

`on_bar` fires at every completed 5-min RTH bar (dt = bar completion, real ET);
`bars_so_far` = {symbol: completed bars}. `SpreadOrder(symbol, structure,
short_delta|strike_offset, width, dte_target, tp_pct, stop_mult,
time_exit_dte, eod_flatten, max_hold_days)`; structures: bull_put, bear_call,
iron_condor, long_call, long_put, calendar.

### Pricing / fills (sim_proposed conventions kept)

- Option legs priced from Polygon 5-min bars; structure value at a bar =
  intersection-aligned sum of `side × close` across legs.
- Entry fill = close of first option bar starting at/after the signal,
  within a 30-min window, ± per-leg half-spread; exit fills likewise.
- Slippage: per-symbol empirical NBBO half-spread per leg from
  `sim_proposed.build_slippage_model` (whalestream medians, floor $0.05) —
  cached in `mech_iv_slip_cache.json` (one-time ~1 min CSV parse).
- Commission $0.65/leg/contract round-trip (= sim_proposed's $1.30 per 2-leg
  spread; iron condor pays $2.60).
- Expiration fallback = intrinsic settlement from `underlying_daily_closes.csv`
  (no exit slippage on cash settle).

### Proxy choices (documented per task)

1. **Delta** = `sim_proposed.bs_delta` with sigma = per-symbol **daily median
   alert IV** (`build_iv_daily`), most recent session **strictly before** entry
   date (point-in-time safe). Alert-median IV skews high vs true ATM IV
   (alerts cluster in OTM/short-dated contracts), so realized strikes sit
   slightly further OTM than the nominal delta target. For 0DTE, dte =
   fraction of the trading day remaining to 16:00 ET (floor 0.01 day).
2. **Strikes**: Tradier strikes cache consulted first, but ~99% of the 4,580
   cached `tradier:strikes_*` entries are **empty lists** (poisoned by the
   legacy engine caching empty responses), and Tradier cannot serve historical
   expirations — so the workhorse is a synthetic grid: $1 for SPY/QQQ/IWM
   (their true near-the-money increment), price-scaled for others. No live
   Tradier calls are made.
3. **Expiry selection**: dte_target=0 → same-day (SPY/QQQ had daily expiries
   all of 2025); otherwise date+dte advanced to Friday, stepped back one day
   if the Friday is a holiday (Good Friday weeks).
4. **Calendar** structure is implemented (short near / long far, same strike,
   debit-style, max loss = debit) but approximate and not exercised by the
   reference strategies.

### Portfolio (same as sim_proposed)

$10k account, 5% risk/trade sized against structure max loss, max 3
concurrent, 2% daily-loss halt on new entries, deterministic exit resolution
at entry time.

### Coverage instrumentation

Every option-bar lookup is **per (contract, ET day)** — keys exactly match the
archive's Databento-format daily keys (`polygon:bars_{OCC}_{d}_{d}_5_minute`)
so overlay reuse is maximal — and is classified as `archive` (E:\), `overlay`
(local DB), `api_fresh` (live pull, data returned), or `api_empty` (live pull,
no bars). A hard budget guard aborts the run if fresh calls exceed 2,500.

## 3. Reference strategies

- `spy_putwrite_weekly`: Monday 10:00 ET, sell SPY 30-delta put spread,
  $5 wide, nearest weekly expiry ≥ 4 DTE (Friday), TP 50%, stop 2.0×,
  time exit 1 DTE.
- `qqq_0dte_ic_1100`: daily 11:00 ET, sell QQQ 0DTE iron condor, ~15-delta
  shorts, $5 wings, EOD flatten (15:50 ET bar), no TP/stop.

## 4. Smoke results — 2025-06 (20 sessions), one portfolio per strategy

| strategy | n | WR | PF | P&L | max DD | exits |
|---|---|---|---|---|---|---|
| spy_putwrite_weekly | 5 | 80% | 29.2 | **+$177.50** | $6 | 4 TP, 1 time-exit-1DTE |
| qqq_0dte_ic_1100 | 20 | 35% | 0.18 | **−$634.00** | $652 | 18 EOD flatten, 2 flatten-at-last-bar |

Sanity checks (these are what the smoke was for — neither strategy is a
recommendation from one month):

- SPY realized short deltas 0.287–0.317 vs 0.30 target; QQQ shorts 0.109–0.190
  vs 0.15 — the delta targeting lands where it should despite the alert-IV
  proxy. Entries fire at exactly 10:00/11:00 ET; expiries resolve correctly
  (incl. Jun-30 → Jul-3 short week and the Juneteenth session gap).
- QQQ ICs collected small credits (median ≈ $0.32 on $5 wings after $0.20
  slippage) and lost on trend days (worst: Jun-5, −$310 when the call side
  went ITM into the close) — economically the *expected* shape for naive
  held-to-close 0DTE condors, i.e. the pricing pipeline isn't fantasy-filling.
- 2 of 20 QQQ days exited at the last aligned bar (~15:40) instead of the
  15:50 flatten bar because a far wing stops printing — logged distinctly
  (`eod_flatten_lastbar`) so thin-liquidity exits are auditable.
- Determinism/reuse check: an immediate rerun reproduced both result sets
  exactly with **0 fresh API calls** (100% cache hit; overlay serves what the
  first pass pulled). Artifacts: `trades_mech_smoke_202506_*.csv`,
  `summary_mech_smoke_202506.json` (+ `*_rerun_*` from the verification pass).

## 5. Coverage + full-year budget

Per (contract, ET-day) option-bar series for the June smoke:

| strategy | day-keys needed | archive (E:\) | overlay | fresh API | missing/empty |
|---|---|---|---|---|---|
| spy_putwrite_weekly | 46 | 10 (21.7%) | 0 | 36 (78.3%) | **0** |
| qqq_0dte_ic_1100 | 80 | 10 (12.5%) | 0 | 70 (87.5%) | **0** |

- **The archive barely covers mechanical strategies** (~12–22%): its keys are
  flow-alert-conditioned and (for QQQ 0DTE) clustered on $5-multiple strikes,
  while delta-targeted entries land on $1-grid strikes. Plan on ~80–90% of
  contract-days being fresh pulls the first time any strategy touches a month.
- **No gaps**: every needed series existed on Polygon (0 empty responses), and
  each contract-day cost exactly 1 successful request (no pagination).
- **API cost per simulated month** (first run): ~36 calls/month for the weekly
  SPY 2-leg spread, ~70 calls/month for the daily 4-leg QQQ 0DTE IC —
  ~106/month for both. **Full-year first-run estimate: ~1,270 fresh calls**
  (≈ 430/yr weekly-spread-style, ≈ 840/yr daily-4-leg-style), comfortably
  inside a 3,000-call budget; reruns are free. Underlying bars are already
  fully prefetched (24 calls covered all 500 SPY/QQQ session-days).
- **Time, not count, is the real budget**: the Polygon/Massive key is heavily
  429-throttled (instant 429s observed mid-day; effective throughput ~5
  successful calls/min through the engine's 12s-backoff retry). June's 106
  fresh calls took ~21 min wall clock; a full-year first run for these two
  strategies ≈ 4–5 h of paced pulling (then minutes from cache). The key
  appears quota-contended (the droplet presumably shares it), so schedule
  full-year prefetches off-hours.
- This task's totals: 24 (underlying prefetch) + 106 (option bars) = **130
  fresh API calls**, vs the ~3,000 cap. E:\ archive and droplet untouched.

## 6. Caveats / known limits

1. Alert-IV sigma is biased high vs ATM IV; delta targets are approximate
   (observed error ≈ ±0.04 delta). Good enough for strike selection, not for
   greeks-accurate analytics.
2. Close-price fills ± static half-spread; no queue/partial-fill modeling;
   0DTE half-spreads near the close are likely wider than the $0.05 floor —
   treat 0DTE P&L as optimistic.
3. Multi-leg marks use timestamp-intersection of leg bars; far wings print
   sparsely, thinning the mark series (n_aligned_bars is recorded per trade).
4. Engine `ts_to_date`/`ts_to_time` (fixed UTC-5) are NOT used here; if other
   tooling joins on those fields, expect a 1h skew Mar–Nov.
5. Synthetic $1 strike grid for SPY/QQQ/IWM matches their listed increments;
   for other symbols the price-scaled grid is a guess — verify before using.
