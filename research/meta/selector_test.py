"""Does a daily 'pick the best-recently-performing strategy' selector beat just holding all
strategies equally? Tests the user's meta-bot idea: run K strategies, each day pick the one
that looks most lucrative (trailing performance), trade it. Walk-forward, no lookahead.

If selection captures real edge -> selector >> equal-weight. If it's chasing noise (a max over
noisy estimates with no persistence) -> selector <= equal-weight. A time-shuffle null + a
perfect-foresight ceiling bound the result.
"""
import json
import numpy as np
import pandas as pd

RNG = np.random.default_rng(20260625)
W = 10          # trailing window for the "looks lucrative" score
MIN_TRAIL = 5   # min trailing observations to be eligible

o = pd.read_parquet("research/intraday/intraday_outcomes.parquet")
o["adate"] = o.adate.astype(str).str[:10]
g = pd.DataFrame(json.load(open("research/broad/gex/gex_SPY.json"))["data"])
g["net_gex"] = g.call_gamma.astype(float) + g.put_gamma.astype(float)
gex = dict(zip(g.date, g.net_gex))
o["neg_gex"] = o.adate.map(gex) < 0

# --- 6 candidate strategies (each: daily mean signed return ret_eod) ---
S1 = o.family == "S1"
strats = {
    "sweep_100k_500k": o[S1 & (o.total_premium >= 100_000) & (o.total_premium < 500_000)],
    "sweep_500k_1M":   o[S1 & (o.total_premium >= 500_000) & (o.total_premium < 1_000_000)],
    "sweep_ge_1M":     o[S1 & (o.total_premium >= 1_000_000)],
    "floor_darkpool":  o[o.family == "S2"],
    "sweeps_neg_gex":  o[S1 & o.neg_gex],
    "sweeps_pos_gex":  o[S1 & ~o.neg_gex],
}
daily = pd.DataFrame({name: df.groupby("adate").ret_eod.mean() for name, df in strats.items()})
daily = daily.sort_index()
vol = daily / daily.std()        # vol-normalize so strategies are comparable (edge preserved)
dates = list(vol.index)
print(f"{len(dates)} trading days, {vol.shape[1]} strategies")
print("per-strategy full-sample mean daily ret (bps):",
      {k: round(10000 * daily[k].mean(), 1) for k in daily})


def run_selector(panel):
    sel, eqw, perf, rnd = [], [], [], []
    for i in range(W, len(panel)):
        t = panel.index[i]
        row = panel.loc[t]
        avail = [c for c in panel.columns if not np.isnan(row[c])]
        if not avail:
            continue
        trail = panel.iloc[i - W:i]
        scores = {c: trail[c].mean() for c in avail if trail[c].notna().sum() >= MIN_TRAIL}
        if scores:
            pick = max(scores, key=scores.get)        # the "most lucrative looking" strategy
            sel.append(row[pick])
        eqw.append(np.nanmean([row[c] for c in avail]))
        perf.append(np.nanmax([row[c] for c in avail]))
        rnd.append(row[RNG.choice(avail)])
    return {k: np.array(v) for k, v in dict(selector=sel, equal_weight=eqw,
                                            perfect=perf, random=rnd).items()}


def stats(a):
    a = a[~np.isnan(a)]
    pos, neg = a[a > 0].sum(), -a[a < 0].sum()
    pf = pos / neg if neg > 0 else float("inf")
    return dict(n=len(a), cumret=round(a.sum(), 3), mean_bps=round(10000 * a.mean(), 2),
                pf=round(pf, 2), tstat=round(a.mean() / (a.std() / np.sqrt(len(a))), 2))


res = run_selector(vol)
print("\n=== walk-forward results (vol-normalized daily returns) ===")
for k, v in res.items():
    print(f"  {k:13s}", stats(v))

# time-shuffle null: break any persistence; if selector still 'works', it was noise
shuf = vol.apply(lambda col: pd.Series(RNG.permutation(col.values), index=col.index))
res_null = run_selector(shuf)
print("\n=== time-shuffle null (persistence destroyed) ===")
for k in ("selector", "equal_weight"):
    print(f"  {k:13s}", stats(res_null[k]))

print("\nVERDICT (naive): selector beats equal-weight?",
      "YES" if stats(res["selector"])["cumret"] > stats(res["equal_weight"])["cumret"] else "NO")

# --- the decisive instrument: 300-shuffle null. If the real selector is indistinguishable
#     from a no-information selector, its trailing-performance signal is worthless. ---
real = res["selector"].sum()
null_sel = np.array([run_selector(
    vol.apply(lambda col: pd.Series(RNG.permutation(col.values), index=col.index)))["selector"].sum()
    for _ in range(300)])
p = float((null_sel >= real).mean())
print(f"\n=== 300-shuffle null (the honesty test) ===")
print(f"  real selector cumret = {real:.2f}")
print(f"  shuffled-selector: mean={null_sel.mean():.2f}, 95th pct={np.percentile(null_sel,95):.2f}")
print(f"  p(shuffled >= real) = {p:.3f}")
print("  ->", "selection carries real info (p<0.05)" if p < 0.05
      else "selection is INFORMATION-FREE: picking the best-trailing strategy is no better than chance")
