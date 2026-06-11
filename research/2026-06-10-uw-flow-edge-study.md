# UW Options-Flow Edge Study — 2026-06-10

**Question:** Do Unusual Whales flow alerts contain a tradable, out-of-sample-consistent directional edge that justifies $126/mo and a bot strategy?

**Answer up front: No.** Every slice that looks impressive in-sample fails at least one of three honesty tests: (1) stability across time-thirds, (2) survival after removing the semiconductor-rally tickers, (3) replication on an independent 12-month flow dataset. The strongest in-sample slice (+55 bps/3d, t=4.3) decays to ~0 in the most recent third of the sample, and its pre-registered out-of-sample test comes back at **-15 bps — opposite sign**. Details below.

---

## 1. Data inventory

### 1.1 UW flow alert CSVs (droplet `/root/trading-bot/data/`)

| file | size | rows | date range | unique-new after id-dedupe |
|---|---|---|---|---|
| uw_flow_history_live.csv | 39 MB | 31,215 | 2026-03-04 → 03-09 | 31,215 |
| uw_flow_history_live_b.csv | 885 MB | 664,875 | 2026-03-09 → 06-10 | 664,651 |
| uw_flow_history_live_c.csv | 57 MB | 45,518 | 03-13 → 03-19 | 10 |
| uw_flow_history_paper_g.csv | 220 MB | 174,638 | 03-20 → 04-16 | 43 |
| uw_flow_history_paper_h.csv | 143 MB | 113,931 | 03-30 → 04-16 | 14 |
| uw_flow_history_paper_i.csv | 89 MB | 70,547 | 04-08 → 04-16 | 0 |
| uw_flow_history_paper_j.csv | 44 MB | 34,553 | 04-17 → 04-21 | 17 |
| **union** | | **1,135,277 raw** | **2026-03-04 → 2026-06-10** | **695,950 unique** |

- `live_b` is a superset of essentially everything else; the other six files added only 84 incremental alerts. The local `E:\BanksBackup` file (Mar 5–6) is inside this window and redundant.
- Coverage is continuous: 14 ISO weeks at 22.8k–60.8k alerts/week (partial first/last weeks). 3,185 tickers.
- **Schema break:** 79,503 rows logged 2026-04-27 → 05-06 used a different payload (empty `type`/`strike`/`expiry`/`created_at` columns). Recovered by parsing the OCC code in `raw_json.option_chain` (e.g. `SPY260515P00678000`) and `rule_name`. After recovery, 100% of alerts have type/expiry.
- Composition: 426,859 calls / 269,091 puts. Alert rules: RepeatedHits 73%, AscendingFill 13%, DescendingFill 13%, Floor-type ~0.7%. Median premium $63k; **87% of stock alerts are < $250k premium**. has_sweep 21%, has_floor 0.8%, all_opening_trades 0.2%.

### 1.2 `whalestream_options/` (3.4 GB) — used as the independent validation set

260 daily JSON pulls, **2025-03-03 → 2026-02-27**: 1,849,446 records, **1,034,471 unique after uuid dedupe** (consecutive pulls overlap ~44%). This is a *different product/schema* (trade-cluster alerts with `bid_ask_indicator`, `short_type`, `is_golden`, …) with a built-in **$250k minimum premium** — exactly the premium band where the in-sample UW "edge" lived. It cannot validate UW's specific alert rules, but it directly tests the underlying economic hypothesis ("large aggressive directional options flow predicts short-horizon excess returns") on 12 independent months. Aggregated on-droplet to 111,524 (ticker, ET-date, direction) day-events.

### 1.3 Other
- `tradier_truth_outcomes.csv`: 81 credit spreads the old bot actually traded (Feb–Apr 2026) — used for the bonus join (§7).
- Prices: yfinance adjusted closes. UW window: 1,209 tickers (≥30 alerts → 97.7% of alert volume), 2 download failures. OOS window: 1,009 tickers (≥20 alerts → 99% of volume), 36 failures (delisted/renamed) — survivorship caveat in §6.

## 2. Methodology

**Direction mapping.** `ask_share = total_ask_side_prem / (ask_side + bid_side prem)`. Side = `ask` if ask_share ≥ 0.6, `bid` if ≤ 0.4, else `mixed` (4.9% of alerts, excluded). Direction: **bullish** = call hit at ask (aggressive call buying) or put hit at bid (put selling); **bearish** = put at ask or call at bid. Standard aggressor-side read; 662k alerts get a direction (334k bull / 328k bear). Populations: 469,289 stock+ADR directional alerts (primary study), 173,324 ETF (separate), 19,425 index (excluded from the stock study).

**Event construction.** Timestamps are UTC (`created_at`, falling back to `log_timestamp`; median gap 17 s). Alerts after 16:00 ET roll to the next trading day. Outcomes are close-to-close log returns from alert date D at +1/+3/+5 trading days, all **excess vs SPY**. To kill pseudo-replication (dozens of same-name alerts sharing one outcome), all statistics use **unique (ticker, date, direction) events**, never raw alert counts. Two t-stats: `t_ev` (event-level) and `t_day` (t over daily portfolio means; robust to same-day cross-sectional correlation — the more honest of the two). Hit rate = share of events where sign(excess return) matched flow direction.

**Features:** premium bucket (<250k / 250k–1M / >1M), has_sweep, has_floor, all_opening, moneyness (ITM / ATM 0–2.5% / OTM 2.5–10% / DeepOTM >10%, sign-adjusted by type), DTE (0–3 / 4–14 / 15–45 / 45+), vol/OI (<1 / 1–3 / >3), rule group, repeat-cluster flag (≥3 same-ticker same-direction alerts within 60 min; 75% of directional alerts qualify), market cap, IV bucket, ETF/stock.

**Splits.** H1/H2 at the median alert (2026-04-27); time-thirds for candidates; regime = SPY > 20-day SMA **as of the prior close** (no lookahead; uptrend share 51%).

**Multiple-comparisons discipline.** ~82 slices × 3 horizons + ~45 follow-up/OOS tests ≈ **290 tests**. At |t|≥2, ~14 false positives are expected by pure chance. Gates: n≥200 events, |t_ev|≥2.5, same sign in both halves, **plus** the thirds / ticker-concentration / out-of-sample checks in §5–6.

## 3. Full in-sample slice table (UW alerts, Mar 4 – Jun 10 2026)

Signed excess return in bps (positive = flow direction was right). n = unique ticker-day-direction events with a 3d outcome. H1/H2 = half-sample 3d means; up/down = SPY-regime 3d means. Stock population unless labeled ETF. Full numeric detail incl. 1d/5d splits is in `2026-06-10-uw-flow-slice-results.csv`.


| slice | n(3d) | hit | 3d bps | t_ev | t_day | H1 | H2 | up | down | 1d bps (t) | 5d bps (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ALL stock directional | 63551 | 0.498 | -1 | -0.5 | -1.9 | -3 | +0 | +1 | -4 | -1 (-0.6) | -3 (-0.6) |
| bull (stock) | 31914 | 0.477 | -9 | -2.1 | -0.7 | -8 | -10 | +12 | -36 | -3 (-1.1) | -7 (-1.2) |
| bear (stock) | 31637 | 0.520 | +6 | 1.5 | -0.6 | +3 | +11 | -10 | +26 | +1 (0.2) | +2 (0.3) |
| bull prem <250k | 31022 | 0.478 | -9 | -2.0 | -1.1 | -7 | -11 | +13 | -37 | -3 (-1.0) | -6 (-1.1) |
| bear prem <250k | 30812 | 0.519 | +7 | 1.5 | -0.4 | +2 | +12 | -11 | +27 | +1 (0.3) | +2 (0.3) |
| bull prem 250k-1M | 7136 | 0.502 | +33 | 3.5 | 1.2 | +29 | +36 | +65 | -9 | +18 (3.4) | +59 (4.9) |
| bear prem 250k-1M | 7124 | 0.503 | -20 | -2.2 | -1.2 | -24 | -15 | -42 | +9 | -14 (-2.7) | -34 (-2.8) |
| bull prem >1M | 1661 | 0.516 | +58 | 2.8 | 2.0 | +53 | +63 | +99 | +3 | +29 (2.5) | +121 (4.8) |
| bear prem >1M | 1709 | 0.489 | -40 | -1.9 | -1.4 | -39 | -42 | -99 | +47 | -17 (-1.4) | -80 (-3.1) |
| bull sweep | 12386 | 0.485 | +3 | 0.4 | 0.3 | +8 | -2 | +39 | -43 | -1 (-0.1) | +6 (0.6) |
| bear sweep | 12583 | 0.511 | -6 | -0.9 | -0.4 | -13 | +1 | -33 | +25 | -4 (-1.0) | -14 (-1.5) |
| bull no-sweep | 30158 | 0.478 | -10 | -2.2 | -0.9 | -7 | -12 | +10 | -35 | -2 (-0.7) | -6 (-1.1) |
| bear no-sweep | 29654 | 0.519 | +6 | 1.2 | -0.6 | +2 | +10 | -11 | +26 | +0 (0.2) | +0 (0.0) |
| bull floor | 1347 | 0.508 | +16 | 0.6 | 0.1 | -36 | +91 | +15 | +17 | +12 (0.9) | +25 (0.9) |
| bear floor | 1208 | 0.517 | -7 | -0.3 | -0.1 | -11 | -1 | -19 | +5 | +6 (0.4) | +0 (0.0) |
| bull all_opening | 465 | 0.469 | +7 | 0.2 | -0.2 | -45 | +76 | +32 | -19 | +37 (1.7) | +45 (1.1) |
| bear all_opening | 370 | 0.530 | -27 | -0.6 | -0.9 | -28 | -24 | -42 | -14 | -33 (-1.2) | -12 (-0.2) |
| bull dte 0-3 | 7107 | 0.496 | +5 | 0.4 | 0.2 | +20 | -13 | +38 | -37 | +10 (1.6) | +43 (3.3) |
| bear dte 0-3 | 7428 | 0.500 | -13 | -1.3 | -0.9 | -29 | +5 | -50 | +38 | -13 (-2.2) | -50 (-4.0) |
| bull dte 4-14 | 11001 | 0.488 | -5 | -0.6 | -1.2 | +16 | -27 | +30 | -52 | +0 (0.0) | +6 (0.5) |
| bear dte 4-14 | 11383 | 0.510 | -1 | -0.1 | -0.1 | -11 | +11 | -25 | +30 | +2 (0.4) | -9 (-0.9) |
| bull dte 15-45 | 17979 | 0.483 | -4 | -0.6 | -0.0 | +3 | -12 | +31 | -46 | +2 (0.5) | +7 (0.9) |
| bear dte 15-45 | 18189 | 0.517 | +6 | 1.1 | -0.0 | -7 | +21 | -22 | +37 | -1 (-0.3) | +2 (0.2) |
| bull dte 45+ | 21770 | 0.482 | +4 | 0.8 | 0.8 | -5 | +12 | +31 | -31 | +3 (1.0) | +12 (1.8) |
| bear dte 45+ | 20892 | 0.515 | -6 | -1.1 | 0.1 | -4 | -7 | -27 | +22 | -3 (-1.2) | -14 (-2.1) |
| bull mny ITM | 15987 | 0.483 | -4 | -0.6 | -0.1 | +2 | -10 | +20 | -34 | +0 (0.1) | -2 (-0.3) |
| bear mny ITM | 17191 | 0.518 | +7 | 1.1 | -0.7 | -0 | +15 | -15 | +34 | -0 (-0.1) | +4 (0.5) |
| bull mny ATM(0-2.5%) | 11589 | 0.485 | -3 | -0.4 | -0.7 | +5 | -10 | +27 | -39 | +3 (0.8) | +3 (0.3) |
| bear mny ATM(0-2.5%) | 11519 | 0.519 | -0 | -0.1 | -0.6 | -3 | +2 | -22 | +26 | -4 (-1.1) | -2 (-0.3) |
| bull mny OTM(2.5-10%) | 17034 | 0.483 | +2 | 0.3 | -0.2 | +9 | -4 | +26 | -29 | +2 (0.6) | +7 (0.8) |
| bear mny OTM(2.5-10%) | 16219 | 0.511 | -1 | -0.2 | 0.5 | -13 | +12 | -18 | +19 | -2 (-0.6) | -8 (-1.0) |
| bull mny DeepOTM(>10%) | 17701 | 0.490 | +4 | 0.7 | -0.4 | +3 | +4 | +45 | -51 | +5 (1.2) | +20 (2.4) |
| bear mny DeepOTM(>10%) | 16595 | 0.507 | -11 | -1.6 | -0.5 | -18 | -2 | -53 | +40 | -3 (-0.9) | -24 (-2.8) |
| bull voloi <1 | 26884 | 0.476 | -11 | -2.2 | 0.3 | -8 | -13 | +16 | -44 | -3 (-1.0) | -5 (-0.7) |
| bear voloi <1 | 26856 | 0.518 | +5 | 1.1 | -0.6 | +0 | +12 | -14 | +29 | -1 (-0.2) | -1 (-0.1) |
| bull voloi 1-3 | 11862 | 0.494 | +13 | 1.7 | 0.1 | +21 | +4 | +45 | -26 | +8 (1.7) | +26 (2.6) |
| bear voloi 1-3 | 11489 | 0.512 | -3 | -0.3 | -0.7 | -8 | +4 | -30 | +31 | -7 (-1.5) | -10 (-1.0) |
| bull voloi >3 | 12661 | 0.495 | +14 | 1.9 | -0.9 | +17 | +12 | +49 | -31 | +9 (2.0) | +24 (2.6) |
| bear voloi >3 | 12340 | 0.508 | -8 | -1.0 | -0.4 | -9 | -6 | -31 | +20 | -5 (-1.2) | -15 (-1.6) |
| bull rule RepeatedHits | 28419 | 0.479 | -6 | -1.3 | 0.2 | -4 | -10 | +17 | -35 | -1 (-0.4) | -3 (-0.4) |
| bear rule RepeatedHits | 28302 | 0.518 | +6 | 1.2 | 0.5 | +4 | +8 | -11 | +26 | +1 (0.3) | +1 (0.1) |
| bull rule AscendingFill | 10893 | 0.486 | +9 | 1.1 | -0.1 | +12 | +6 | +45 | -39 | +1 (0.2) | +21 (2.0) |
| bear rule AscendingFill | 9989 | 0.503 | -21 | -2.5 | -1.3 | -23 | -17 | -61 | +31 | -16 (-3.3) | -36 (-3.4) |
| bull rule DescendingFill | 10556 | 0.484 | -3 | -0.4 | -0.9 | +7 | -12 | +26 | -42 | +1 (0.2) | +11 (1.1) |
| bear rule DescendingFill | 10581 | 0.513 | -0 | -0.0 | -1.1 | -18 | +16 | -35 | +44 | -2 (-0.4) | -11 (-1.1) |
| bull rule FloorRule | 1347 | 0.508 | +16 | 0.6 | 0.1 | -36 | +91 | +15 | +17 | +12 (0.9) | +25 (0.9) |
| bear rule FloorRule | 1208 | 0.517 | -7 | -0.3 | -0.1 | -11 | -1 | -19 | +5 | +6 (0.4) | +0 (0.0) |
| bull cluster | 11028 | 0.492 | +11 | 1.3 | -0.3 | +16 | +4 | +53 | -46 | +9 (1.8) | +22 (2.1) |
| bear cluster | 10792 | 0.502 | -10 | -1.2 | -1.0 | -18 | +0 | -48 | +39 | -10 (-2.0) | -16 (-1.5) |
| bull no-cluster | 27804 | 0.475 | -10 | -2.2 | -0.5 | -10 | -10 | +10 | -35 | -3 (-1.2) | -7 (-1.1) |
| bear no-cluster | 27726 | 0.522 | +9 | 2.0 | 0.3 | +5 | +13 | -4 | +25 | +2 (0.8) | +4 (0.6) |
| bull mcap <2B | 2968 | 0.455 | -53 | -2.4 | -0.9 | -45 | -65 | +47 | -147 | -0 (-0.0) | -55 (-1.9) |
| bear mcap <2B | 2791 | 0.538 | +48 | 2.0 | 0.2 | +24 | +79 | -11 | +100 | -4 (-0.3) | +42 (1.4) |
| bull mcap 2-10B | 7680 | 0.482 | -16 | -1.6 | -1.0 | +14 | -61 | +31 | -60 | -5 (-0.9) | +5 (0.4) |
| bear mcap 2-10B | 7596 | 0.515 | +11 | 1.1 | 0.1 | -20 | +60 | -29 | +47 | -0 (-0.0) | -4 (-0.3) |
| bull mcap 10-100B | 11673 | 0.476 | -19 | -3.1 | 0.1 | -13 | -28 | -25 | -13 | -5 (-1.6) | -21 (-2.8) |
| bear mcap 10-100B | 11863 | 0.521 | +18 | 3.1 | -0.5 | +10 | +30 | +26 | +11 | +5 (1.4) | +18 (2.4) |
| bull mcap >100B | 4990 | 0.489 | +3 | 0.5 | 1.6 | -9 | +23 | -4 | +11 | +2 (0.6) | +8 (0.9) |
| bear mcap >100B | 4908 | 0.510 | -6 | -0.8 | -1.8 | +5 | -23 | -0 | -10 | -4 (-0.9) | -19 (-2.2) |
| bull iv IV<30 | 3138 | 0.471 | -2 | -0.2 | -0.6 | -9 | +7 | -25 | +24 | +10 (1.8) | -34 (-3.0) |
| bear iv IV<30 | 3022 | 0.541 | +2 | 0.2 | 0.3 | +1 | +5 | +27 | -27 | -13 (-2.2) | +33 (2.7) |
| bull iv IV30-60 | 13012 | 0.483 | -11 | -2.7 | -1.4 | -20 | +3 | -41 | +15 | -2 (-0.7) | -20 (-3.8) |
| bear iv IV30-60 | 13153 | 0.512 | +11 | 2.5 | 1.3 | +18 | -2 | +43 | -16 | +0 (0.1) | +19 (3.4) |
| bull iv IV>60 | 15530 | 0.481 | -18 | -2.4 | -0.3 | +8 | -56 | +38 | -70 | -1 (-0.3) | +6 (0.6) |
| bear iv IV>60 | 15317 | 0.519 | +16 | 2.1 | -0.9 | -15 | +63 | -32 | +60 | +0 (0.0) | -7 (-0.7) |
| bull pure-ask (share=1) | 22895 | 0.479 | -7 | -1.3 | -1.1 | -2 | -11 | +23 | -45 | +1 (0.4) | +3 (0.5) |
| bear pure-ask (share=1) | 16683 | 0.515 | +5 | 0.8 | -0.1 | +1 | +9 | -13 | +26 | +0 (0.0) | -9 (-1.2) |
| bull pure-bid (put@bid) | 15122 | 0.493 | +11 | 1.8 | 0.8 | +14 | +6 | +39 | -25 | +5 (1.3) | +16 (2.0) |
| bear pure-bid (call@bid) | 21732 | 0.516 | -1 | -0.3 | 0.3 | -5 | +4 | -31 | +36 | -3 (-0.8) | -11 (-1.6) |
| ETF bull | 4703 | 0.450 | -65 | -6.7 | -2.9 | -30 | -129 | -67 | -64 | -18 (-3.1) | -105 (-8.4) |
| ETF bear | 4860 | 0.529 | +51 | 5.1 | 0.1 | +11 | +125 | +51 | +52 | +17 (2.8) | +88 (6.9) |
| bull sweep prem>1M | 555 | 0.539 | +50 | 1.2 | 0.8 | +24 | +69 | +65 | +24 | +20 (0.9) | +106 (2.1) |
| bear sweep prem>1M | 555 | 0.465 | -28 | -0.7 | -0.5 | +27 | -71 | -79 | +45 | -4 (-0.2) | -111 (-2.2) |
| bull sweep OTM dte4-45 | 3797 | 0.496 | +20 | 1.4 | 0.6 | +45 | -5 | +84 | -58 | -2 (-0.2) | +35 (2.0) |
| bear sweep OTM dte4-45 | 3547 | 0.504 | -3 | -0.2 | -0.2 | -33 | +25 | -60 | +62 | +5 (0.6) | -18 (-0.9) |
| bull voloi>3 OTM dte4-45 | 3597 | 0.502 | +36 | 2.6 | 1.1 | +50 | +19 | +81 | -20 | +19 (2.3) | +42 (2.4) |
| bear voloi>3 OTM dte4-45 | 3504 | 0.503 | -9 | -0.7 | -0.4 | -1 | -19 | -38 | +23 | -7 (-0.8) | -5 (-0.3) |
| bull cluster prem>250k | 4604 | 0.516 | +55 | 4.3 | 2.0 | +46 | +63 | +103 | -9 | +28 (3.8) | +87 (5.3) |
| bear cluster prem>250k | 4613 | 0.487 | -38 | -3.0 | -1.2 | -50 | -27 | -76 | +14 | -22 (-3.1) | -60 (-3.8) |
| bull smallcap<2B sweep | 830 | 0.446 | -79 | -1.5 | -1.3 | -79 | -79 | -4 | -141 | -42 (-1.4) | -188 (-3.1) |
| bull pure-ask voloi>3 prem>250k | 1605 | 0.526 | +51 | 2.4 | 1.8 | +31 | +69 | +104 | -15 | +51 (4.4) | +107 (4.1) |
| bear pure-ask voloi>3 prem>250k | 1576 | 0.490 | +7 | 0.3 | -0.5 | +2 | +14 | -2 | +21 | -4 (-0.3) | -32 (-1.1) |

**Reading the table honestly:** at the aggregate level UW flow has **zero** directional information (hit rate 49.8%, -1 bps). Flow *direction itself* is mildly anti-predictive for bulls (-9 bps) because the population is dominated by sub-$250k retail-sized prints on high-attention names that mean-revert. The only monotone, internally consistent in-sample structure is **premium size on the bull side** (<250k: -9 -> 250k-1M: +33 -> >1M: +58 bps @3d) and its combinations (cluster + >250k: +55 bps, t_ev 4.3; pure-ask + vol/OI>3 + >250k: +51 bps). Large bearish flow shows the mirror image (negative signed = stocks *rose* after big bearish flow), consistent with the April post-mortem's "bearish flow in uptrends is hedging noise." The ETF-bull slice is strongly negative (-65 bps, t -6.7) - bullish ETF flow as a contrarian signal. These were the candidates. They all die in sections 4-5.

## 4. Candidate deep-dives (in-sample robustness)

### 4.1 Time-thirds: the "edge" decays to zero inside the sample

3d signed excess, thirds of each slice chronologically:

| slice | T1 (~Mar-mid-Apr) | T2 (~mid-Apr-mid-May) | T3 (~mid-May-Jun) |
|---|---|---|---|
| bull prem 250k-1M | +62 (t 4.5) | +49 (t 2.7) | **-12 (t -0.7)** |
| bull prem >1M | +118 (t 4.6) | +65 (t 1.5) | **-7 (t -0.2)** |
| bull cluster prem>250k | +99 (t 5.5) | +63 (t 2.6) | **+2 (t 0.1)** |
| bear prem >250k (contrarian) | -67 (t -5.4) | -24 (t -1.4) | **+34 (t 2.1) - sign flip** |
| ETF bull (contrarian) | -3 (t -0.2) | -45 (t -2.9) | -148 (t -7.7) - grows, but absent in T1 |

The H1/H2 halves looked consistent only because the strong middle period (T2) straddles the median split. No candidate is sign-stable across all three thirds.

### 4.2 Ticker concentration: it was the semiconductor rally

Top-5 contributors to every bull big-premium slice are the same names: **MRVL, INTC, SNDK, MU, AMD** (the Mar-May 2026 semi/memory run, with NVDA/AVGO/STX just behind).

| slice | full mean @3d | excl. top-5 tickers | excl. semis (26-name list) |
|---|---|---|---|
| bull prem >1M | +58 (t 2.9) | **+16 (t 0.7)** | - |
| bull prem 250k-1M | +33 (t 3.5) | +17 (t 1.8) | - |
| bull cluster prem>250k | +55 (t 4.3) | +32 (t 2.4) | - |
| bull prem>250k, uptrend only | +62 (t 4.9) | - | **+23 (t 1.7)** |
| bear prem>250k, uptrend (fade) | -41 (t -3.3) | - | **-4 (t -0.3)** |

Sector breakdown of "bull prem>250k in uptrend" @3d: Technology **+134 bps** (n=1,445) vs Financials +23, Comms -16, Industrials -43, Consumer Cyclical -54, Healthcare -88, Energy -124, Materials -142. The signal is not "smart flow"; it is "semis went up and attracted flow."

### 4.3 Regime is confounded with calendar

SPY's 20d-SMA regime over the sample is three contiguous blocks: below (Mar 4 - Apr 9), **above (Apr 12 - May 24)**, below (May 31 - Jun 10). The single uptrend block *is* the semi-rally window, so "works in uptrends" and "works on semis in April-May" are the same statement - one regime cycle is not evidence. The explicit hedging-noise test: bearish big-prem flow in uptrends -> -41 bps (fade profitable) **but excl-semis it is -4 bps, i.e. zero**; and in downtrends bearish flow flips sign between the March downtrend (-61, flow was right) and the June downtrend (+104, flow was wrong). Regime conditioning does not rescue any slice; it relabels the calendar.

## 5. Out-of-sample test (whalestream, Mar 2025 - Feb 2026)

Hypotheses were **pre-registered from the in-sample winners before computing OOS results**; same event/stat machinery (day-level events, premium >= 250k by construction; direction from `bid_ask_indicator` A/AA/TA = buy, B/BB/TB = sell; 91.8% return coverage; uptrend share 65%). Pass gates: same sign as in-sample, |t_ev| >= 2.5, sign-consistent across OOS halves.

| pre-registered hypothesis (in-sample claim) | OOS n | OOS mean @3d | t_ev | t_day | OOS H1 / H2 | quarters | verdict |
|---|---|---|---|---|---|---|---|
| H1 bull big-prem stock (+33..+58) | 42,383 | **-9.9** | -2.9 | -0.8 | -3 / -17 | -19/+11/-23/-8 | **FAIL (opposite sign)** |
| H2 bull big-prem, >=3 same-dir alerts/day (+55) | 17,133 | **-15.4** | -2.9 | -0.6 | -11 / -20 | -52/+30/-16/-24 | **FAIL (opposite sign)** |
| H3 = H2 in uptrend (+103) | 10,972 | -4.1 | -0.6 | -0.1 | +5 / -10 | +6/+46/+15/-84 | **FAIL (no effect, unstable)** |
| H4 bear big-prem in uptrend, expect NEG fade (-41) | 27,631 | **+10.9** | +2.5 | +0.8 | +4 / +16 | +4/-26/+11/+54 | **FAIL (opposite sign)** |
| H5 ETF bull, expect NEG contrarian (-65) | 8,058 | +2.6 | +0.4 | +0.7 | -12 / +18 | -24/-0/-22/+57 | **FAIL (no effect)** |

5/5 pre-registered hypotheses fail; three with the **opposite** sign. Also instructive: plain "bear big-prem" was **+16 bps (t 4.8)** in 2025 - bearish flow mildly *predictive* - versus **-19 bps (contrarian)** in the 2026 sample. The same feature flips sign across adjacent windows. That is the signature of regime-driven noise, not of an edge with an economic mechanism.

5d horizons confirm everything above (H1 -17 bps, H2 -25 bps OOS). Secondary OOS probes (golden, unusual, sweep-dominant, $1M+ days) are all <=0 or sign-flipping; see `2026-06-10-uw-flow-oos-results.csv`.

## 6. Overfitting and validity caveats

1. **~290 tests were run.** The slice table alone guarantees several |t|>2.5 entries by chance. Nothing survived the pre-registered OOS gate, which is the only test that controls this.
2. **The 2026 sample is 3.2 months - one market regime cycle.** Uptrend/downtrend, time-thirds, and the semi rally are mutually confounded; this data alone cannot distinguish "flow alpha in uptrends" from "long semis in April."
3. **The OOS dataset is a different alert generator** (whalestream pulls, $250k floor, day-level aggregation, bid/ask-indicator direction). A UW-specific micro-edge (e.g., something in UW's exact RepeatedHits trigger at sub-minute granularity) is not strictly ruled out - but the in-sample candidates were premium/direction effects that this data does test, and they fail.
4. **Survivorship:** ~36 OOS tickers (delisted/renamed, e.g. HES, DFS, PARA, WBA) lack yfinance data and drop out; the bias is small and, if anything, inflates bull-side returns - true OOS results are likely slightly worse for the bull hypotheses.
5. Close-to-close execution (enter at alert-day close) ignores intraday alpha between alert time and close; an intraday study could in principle find structure this design misses - but the old bot traded multi-day holds, so this design matches the actual use case.
6. No earnings-date exclusion; big-premium flow clusters before earnings, so part of the in-sample T1/T2 pop is earnings lottery tickets.
7. All returns are pre-cost. Even the best in-sample slice (+55 bps over 3 days on the *stock*) is thin; expressed via options it would be swamped by spread-crossing costs at retail size.

## 7. Bonus: what the bot actually traded (tradier_truth_outcomes.csv)

81 credit spreads, Feb 27 - Apr 2026: **total P&L -$1,405**, win rate 55.6%, mean -$17.3/trade (classic short-premium profile: many small wins, larger losses). 40/81 trades coincided with same-day, same-direction big-premium flow events; those did not perform meaningfully differently (mean -$14.0, 60% win) from non-matching trades (-$20.6, 51% win). The flow filter did not separate the bot's winners from losers.

## 8. Verdict

**(a) Does UW flow show a robust conditional edge worth $126/mo? No.** The aggregate signal is a coin flip (49.8% hit, -1 bps excess). The conditional slices that clear in-sample significance gates (bull premium >$250k, clustered; large-flow fade) are explained by semiconductor-sector concentration in a single 6-week rally, decay to zero in the final third of the sample, and **fail - mostly with opposite sign - on 12 months of independent out-of-sample flow data**. The hedging-noise finding from the April post-mortem is confirmed in-sample but is *also* sector-driven and does not replicate OOS, so it cannot be inverted into a fade strategy either.

**(b) Entry-filter definition if yes:** Not applicable - no filter met the pre-registered robustness bar. For the record, the best in-sample candidate was: *stock (non-ETF/index), bullish aggressor flow (call with >=60% ask-side premium), total premium >= $250k, >=3 same-direction alerts within 60 min, SPY > 20d SMA, hold 3-5 days* (+55 -> +87 bps in-sample). **Do not deploy it**: its OOS replication is -15 bps (t -2.9). It is recorded here so it is not rediscovered and re-overfit later.

**(c) Plain statement:** Based on 696k deduplicated UW alerts (Mar-Jun 2026) and 1.03M independent flow alerts (Mar 2025 - Feb 2026), there is **no out-of-sample-consistent directional edge in this data at daily horizons**. Cancel the $126/mo subscription unless it is kept for discretionary/situational-awareness value rather than systematic signal value. Any future flow-based strategy should be required to pass exactly the gates used here - sign-consistent time-thirds, sector-concentration exclusion, and a pre-registered hold-out - *before* a dollar is risked.

---

### Artifacts (all in this directory)
- `2026-06-10-uw-flow-slice-results.csv` - full per-slice metrics (1/3/5d, halves, regimes)
- `2026-06-10-uw-flow-oos-results.csv` - pre-registered + secondary OOS results
- `uw_reduced.csv.gz` (696k alerts), `ws_daydir.csv.gz` (111k OOS day-events), `alerts_panel.parquet`, `prices*.parquet`
- Scripts: `reduce_uw.py`, `ws_reduce.py` (droplet-side, read-only), `01_explore.py` ... `06_oos_ws.py`
