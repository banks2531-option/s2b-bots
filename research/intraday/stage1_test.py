"""Stage 1 test — does whale flow predict intraday underlying direction OOS?

Protocol (pre-registered, research/2026-06-14-intraday-flow-edge-prereg.md):
  - S1: tune P_min on H1 over {100k,250k,500k,1M} maximizing H1 mean ret_60
    s.t. >=500 H1 events; freeze; evaluate H2 once.
  - S2 (floor/dark-pool): no tuning; evaluate H2.
  - Honesty: shuffled-direction null, day-block bootstrap 95% CI, premium-decile
    monotonicity, effective-n (events + day-blocks), multiple-comparison ledger.
  - Gate (S1 primary, on H2): see §6 of the prereg.
"""
import os, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUTCOMES = os.path.join(HERE, "intraday_outcomes.parquet")
RESULTS = os.path.join(HERE, "stage1_results.json")
TAU = 0.0025                       # 0.25% economic floor
P_GRID = [100000, 250000, 500000, 1000000]
HORIZONS = ["30", "60", "120", "eod"]
N_BOOT = 2000
RNG = np.random.default_rng(20260614)

def day_block_bootstrap(df, col):
    """95% CI of the mean via resampling (ticker, entry_date) blocks."""
    d = df[["ticker", "adate", col]].dropna(subset=[col])
    if len(d) < 5:
        return (np.nan, np.nan, np.nan, 0, 0)
    blocks = [g[col].to_numpy() for _, g in d.groupby(["ticker", "adate"])]
    nb = len(blocks)
    means = np.empty(N_BOOT)
    for i in range(N_BOOT):
        idx = RNG.integers(0, nb, nb)
        means[i] = np.concatenate([blocks[j] for j in idx]).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(d[col].mean()), float(lo), float(hi), len(d), nb)

def tstat(x):
    x = x[~np.isnan(x)]
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 1 and x.std() > 0 else np.nan

def shuffled_direction_null(rets, real_mean):
    """p = fraction of random-sign means >= real_mean (one-sided)."""
    r = rets[~np.isnan(rets)]
    null = np.array([(r * RNG.choice([-1.0, 1.0], len(r))).mean() for _ in range(N_BOOT)])
    return float((null >= real_mean).mean()), float(np.percentile(null, 95))

def summarize(df, label):
    out = {"label": label, "n_events": int(len(df))}
    for h in HORIZONS:
        col = f"ret_{h}"
        mean, lo, hi, n, nb = day_block_bootstrap(df, col)
        out[col] = {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n": n, "n_blocks": nb,
                    "tstat": tstat(df[col].to_numpy())}
    return out

def premium_decile_monotonic(df):
    d = df.dropna(subset=["ret_60"]).copy()
    if d["total_premium"].nunique() < 10:
        return {"ok": None, "note": "too few distinct premiums"}
    d["dec"] = pd.qcut(d["total_premium"], 10, labels=False, duplicates="drop")
    by = d.groupby("dec")["ret_60"].mean()
    top, med = float(by.iloc[-1]), float(by.median())
    return {"by_decile": {int(k): float(v) for k, v in by.items()},
            "top": top, "median": med, "ok": bool(top >= med)}

def main():
    df = pd.read_parquet(OUTCOMES)
    res = {"tau": TAU, "n_boot": N_BOOT, "families": {}, "ledger_cells": "2 fam x 4 horizons x {real,null}=16"}

    # ---- S1: tune P_min on H1 ----
    s1 = df[df.family == "S1"]
    h1 = s1[s1.half == "H1"]
    tuning = []
    best = None
    for p in P_GRID:
        sub = h1[h1.total_premium >= p]
        m = sub["ret_60"].mean() if len(sub) >= 500 else np.nan
        tuning.append({"P_min": p, "h1_n": int(len(sub)), "h1_mean_ret60": (float(m) if not np.isnan(m) else None),
                       "eligible": bool(len(sub) >= 500)})
        if len(sub) >= 500 and (best is None or m > best[1] or (m == best[1] and p > best[0])):
            best = (p, m)
    p_star = best[0] if best else P_GRID[0]
    res["S1_tuning"] = {"grid": tuning, "P_min_star": p_star,
                        "rule": "max H1 mean ret_60 s.t. h1_n>=500, ties->larger P_min"}

    # ---- evaluate frozen S1 on H2 (OOS) ----
    s1f = s1[s1.total_premium >= p_star]
    s1_h2 = s1f[s1f.half == "H2"]
    s1_summary = summarize(s1_h2, f"S1 OOS (P_min={p_star})")
    real60 = s1_summary["ret_60"]["mean"]
    null_p, null95 = shuffled_direction_null(s1_h2["ret_60"].to_numpy(), real60)
    s1_summary["null_p_ret60"] = null_p
    s1_summary["null_95pct_ret60"] = null95
    s1_summary["premium_decile"] = premium_decile_monotonic(s1_h2)
    s1_summary["h1_descriptive"] = summarize(s1f[s1f.half == "H1"], "S1 H1 (in-sample)")
    res["families"]["S1"] = s1_summary

    # ---- S2 floor/dark-pool: no tuning, evaluate H2 ----
    s2 = df[df.family == "S2"]
    s2_h2 = s2[s2.half == "H2"]
    s2_summary = summarize(s2_h2, "S2 OOS (floor/dark-pool)")
    r2 = s2_summary["ret_60"]["mean"]
    p2, n95_2 = shuffled_direction_null(s2_h2["ret_60"].to_numpy(), r2)
    s2_summary["null_p_ret60"] = p2
    s2_summary["null_95pct_ret60"] = n95_2
    s2_summary["h1_descriptive"] = summarize(s2[s2.half == "H1"], "S2 H1 (in-sample)")
    res["families"]["S2"] = s2_summary

    # ---- GATE (S1 primary, on H2) ----
    r60 = s1_summary["ret_60"]
    cond = {
        "1_ret60_pos_ci": bool(r60["mean"] is not None and r60["mean"] > 0 and r60["ci_lo"] is not None and r60["ci_lo"] > 0),
        "2_econ_floor": bool(any((s1_summary[f"ret_{h}"]["mean"] or -9) >= TAU for h in HORIZONS)),
        "3_sign_consistent": bool((s1_summary["ret_60"]["mean"] or -9) > 0 and (s1_summary["ret_eod"]["mean"] or -9) > 0),
        "4_beats_null": bool(null_p < 0.05),
        "5_premium_monotone": bool(s1_summary["premium_decile"].get("ok") is True),
    }
    cond["PASS"] = all(cond[k] for k in ["1_ret60_pos_ci", "2_econ_floor", "3_sign_consistent", "4_beats_null", "5_premium_monotone"])
    res["GATE_S1"] = cond
    res["VERDICT"] = "PASS -> Stage 2" if cond["PASS"] else "ABANDON (S1 fails gate)"

    json.dump(res, open(RESULTS, "w"), indent=2, default=str)
    print(json.dumps(res, indent=2, default=str)[:4000])
    print(f"\n--> {RESULTS}")
    print("VERDICT:", res["VERDICT"])

if __name__ == "__main__":
    main()
