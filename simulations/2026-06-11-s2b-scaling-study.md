# S2b scaling study — $20k extraction frontier (2026-06-11)

Scaling study for the validated candidate **S2b** (Mon 10:00 ET, SPY short
~40Δ put, $10-wide wing, nearest weekly ≥4 DTE, TP 50% credit, stop 2× credit,
exit 1 DTE; baseline validation: PF 1.86 full / 1.77 H1 / 2.00 H2 / 1.39 at
2× costs, maxDD $481 on $10k — see `2026-06-11-shortlist-validation.md`).

Question: maximum HONEST extraction rate on a **$20k account** via (1) sizing
and (2) more entries. Window = the existing cached year ONLY (2025-03-03 →
2026-02-27; H1 ≤ 2025-08-28 < H2). **Zero Databento**; Phase B data via the
free throttled Polygon key only. Honesty gate: PF ≥ 1.3 in BOTH halves at
baseline costs; worst-week correlation across arms is the key risk metric.

Tooling: `scale_s2b.py` (EquitySim = ShortlistSim + %-of-current-equity
sizing, account $20k) and `variants_data.py` (two-pass miss-enumerate →
paced Polygon fetch). Artifacts: `trades_scale_*.csv`,
`summary_scale_*.json`, `variant_misses.json`, `fetch_variants.log`.

## PHASE A — sizing frontier (existing data, 100% cache-hot, 0 fresh calls)

Sizing semantics (deliberate, documented deviation from the validated
baseline's fixed-$500): risk budget = X% of **current realized equity** at
entry, qty = floor(budget / structure max loss), min 1 contract, max 3
concurrent, harness 2%-daily-loss halt unchanged. Same 47 entries, same
fills/strikes/exits as the validated run — only qty (and commission ×qty)
differs. n=47 / H1 n=24 / H2 n=23 at every level; the lone reject is the
known `no_iv` Monday. Worst week is the same calendar week at every level
(2026-W06, the Feb-2026 time-exit loss), so sizing scales the SAME tail
event, it does not diversify it.

### Baseline costs ($0.05/leg half-spread)

| risk/trade | WR | PF full | PF H1 | PF H2 | P&L (yr) | $/month | maxDD $ | maxDD % eq | worst week | streak |
|---|---|---|---|---|---|---|---|---|---|---|
| 5%   | 80.9% | 1.86 | 1.77 | 2.00 | +$1,709 | **$144** | $481 | 2.2% | −$481 | 1 |
| 7.5% | 80.9% | 1.86 | 1.95 | 1.76 | +$2,890 | **$244** | $963 | 4.1% | −$963 | 1 |
| 10%  | 80.9% | 1.89 | 1.84 | 1.97 | +$3,675 | **$310** | $963 | 4.0% | −$963 | 1 |
| 15%  | 80.9% | 1.75 | 1.72 | 1.78 | +$6,398 | **$539** | $2,407 | 8.6% | −$2,407 | 1 |

### 2× costs

| risk/trade | WR | PF full | PF H1 | PF H2 | P&L (yr) | $/month | maxDD $ | maxDD % eq | worst week | streak |
|---|---|---|---|---|---|---|---|---|---|---|
| 5%   | 76.6% | 1.39 | 1.42 | 1.35 | +$861 | $73 | $501 | 2.4% | −$501 | 3 |
| 7.5% | 76.6% | 1.66 | 1.69 | 1.62 | +$1,698 | $143 | $508 | 2.3% | −$501 | 3 |
| 10%  | 76.6% | 1.40 | 1.51 | **1.25** | +$1,799 | $152 | $1,022 | 4.6% | −$1,003 | 3 |
| 15%  | 76.6% | 1.29 | 1.33 | **1.25** | +$2,539 | $214 | $2,005 | 8.4% | −$2,005 | 3 |

### Phase A readings (honest)

- **Gate check**: ALL four levels pass PF ≥ 1.3 in both halves at baseline
  costs. At 2× costs, 10% and 15% drop to PF ≈ 1.25 in H2 (and 15% full-year
  PF is 1.29) — the calm-half edge is thinner per trade and cost-doubling
  plus larger integer qty erodes it first there.
- **PF wiggle across levels is integer-qty granularity, not signal**: at $20k,
  5% risk = $1,000 vs ~$800 max loss → 1 contract (identical trades to the
  validated $10k run); 7.5% is mostly 1 then 2 as equity compounds; 10% is
  2→3; 15% is 3→5. Different trades get rounded differently, hence
  PF 1.75–1.89. The per-contract edge is one and the same.
- **Risk scales linearly and concentrates in one week**: maxDD ≈ the worst
  single week ≈ one Feb-2026 time-exit loss at every level. 15% risk puts a
  single-week −$2.4k (12% of account) on the table — and that's the realized
  worst in a sample of one year; the structural worst (full $10-wide spread
  loss at 15% × 3 concurrent) is ≈ $9k, 45% of the account.
- Drawdown is measured on end-of-day realized equity (the harness's exit
  resolution is deterministic at entry; no intraday mark-to-market curve).
- The 2%-daily-loss halt never fired at any level (no `daily_halt` rejects):
  with Monday-only entries the week's entry is in before any loss settles.

### Phase A frontier verdict (pre-Phase-B)

The dominant point is **10% risk: ~$310/month on $20k (≈18.4%/yr) with
maxDD 4% and worst week −$963 (4.8% of account)** — same drawdown as 7.5%
(qty rounding) with $66/mo more. It passes both halves at baseline (1.84 /
1.97) but be honest about 2× costs: H2 PF 1.25, $/month falls to $152.
15% risk buys $539/mo but at PF 1.29 full-year under 2× costs it is at the
edge of failing the validation standard outright, with a worst week of
−$2.4k and a structural tail near half the account. Sizing alone cannot
honestly reach ~$500+/mo; that requires more entries (Phase B) or accepting
a config that fails the cost-stress gate.

## PHASE B — frequency/underlying variants (free throttled Polygon)

Data: all 1,252 missing contract-days fetched via the FREE Polygon key in
9.2 min (key ran unthrottled; 0 failures, 0 Databento). All replays
cache-hot, $20k @ 5% risk. Gates: PF ≥ 1.3 in BOTH halves at baseline costs
+ survival at 2× costs.

| variant | n | PF full | H1 | H2 | P&L | maxDD% | worst wk | 2× costs: PF (H1/H2), P&L | verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Mon SPY (S2b anchor)** | 47 | 1.86 | 1.77 | 2.00 | +$1,709 | 2.2 | −$481 | 1.39 (1.42/1.35), +$861 | **PASS (the only one)** |
| V1 Wed SPY | 49 | 1.08 | 1.62 | **0.74** | +$311 | 5.2 | −$542 | 0.79 (1.16/0.54), −$958 | FAIL |
| V1 Fri SPY | 50 | 1.42 | 1.43 | 1.41 | +$1,340 | 3.8 | −$502 | **0.98** (0.84/1.19), −$80 | WEAK — passes halves, dies at 2× costs |
| V2 Mon+Wed+Fri | 141 | 1.29 | 1.56 | **1.06** | +$2,727 | 5.4 | −$1,050 | **0.95** (1.05/0.84), −$557 | FAIL — sign flips under cost stress |
| V3 QQQ Mon | 47 | 1.73 | 2.64 | **1.24** | +$1,554 | 2.2 | −$455 | 1.13 (1.53/0.86), +$338 | WEAK — H2 just under gate; H2 dies at 2× |
| V4 all arms | 185 | 1.27 | 1.67 | **0.99** | +$3,296 | 8.9 | −$1,661 | **0.97** (1.09/0.86), −$380 | FAIL — sign flips under cost stress |

Readings:
- **The weekly edge is concentrated in the Monday entry** (weekend-theta
  capture); Wednesday has no edge in the calm half; Friday and QQQ have
  thinner per-trade margins that doubled costs erase.
- Stacking arms raises raw P&L (+$3,296 on V4) while degrading quality
  below the validation standard (H2 ≈ 1.0, negative at 2× costs) — more
  volume from thinner edges is the legacy failure mode, rejected here by
  the gates.
- Correlation: the combined book's worst week (−$1,661 on V4) shows the
  arms lose together — same index, same crash factor. Extra entries
  diversify income, not risk.

## PHASE B-2 — steep-drawdown extension (Mon-only, user-requested)

Same 47 validated trades, sizing pushed past the prudent band ($20k):

| risk/trade | $/mo | PF | maxDD% | worst wk | 2× costs: $/mo, PF |
|---|---|---|---|---|---|
| 20% | $756 | 1.73 | 10.9 | −$3,369 | $338, 1.34 |
| 25% | $1,070 | 1.76 | 13.6 | −$4,813 | $408, 1.30 |
| 30% | $1,305 | 1.72 | 17.0 | −$6,738 | $489, 1.29 |
| 40% | $1,792 | 1.63 | 22.9 | −$11,070 | $633, 1.26 |

Caveats that bind at this end of the curve: (1) the OBSERVED drawdown is
one year's tail — the STRUCTURAL single-event worst (weekend gap through
the stop to full spread max-loss) realizes ≈ the entire risk allocation:
−20%…−40% of the account in one Monday open; (2) observed-stats full
Kelly ≈ 38%, computed from in-sample-flattered n=47, so true Kelly is
lower; past half-Kelly (~19%) added risk buys mostly variance (visible: PF
erodes 1.86→1.63 as sizing rises); (3) at 2× costs the curve flattens hard
($338→$633/mo for 4× the drawdown).

## PHASE C — recommendation

**Production config: Monday-only S2b at 10% risk/trade on $20k —
~$310/month expected (≈18%/yr), maxDD ~4%, worst observed week −$963
(4.8% of account), structural single-event worst ~−10%.** Passes every
gate with room (PF 1.84/1.97 by half; 1.40 at 2× costs).

Deliberately aggressive book: cap at **20% risk (~$756/mo optimistic,
~$338/mo cost-stressed)**, accepting an observed −11% DD and a structural
−20% single-event tail. Nothing past 25% is defensible on this data
(beyond half-Kelly, flattening stressed returns, one-event tail ≥ 25% of
account).

NOT honestly reachable on $20k from this edge: **$3–5k/month.** Sizing
fails the stress gates before it gets there; added entry arms fail the
calm-half and cost gates. The path to $3–5k/mo remains: this engine at
sane sizing + the futures/prop-eval leg + capital growth.

Weak candidates parked for paper validation only (not in the build):
Friday SPY arm, QQQ Monday arm — both must beat 2×-cost stress on live
paper fills before earning size.
