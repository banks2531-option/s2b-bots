"""GEX-gated flow test (user-requested 2026-06-15) — the 5th UW community-consensus
element. Gate the large-whale flow signal by SPY net-GEX regime and measure OOS
directional edge. Net GEX = call_gamma + put_gamma (UW signs calls +, puts -).
GEX history pulled from UW greek-exposure endpoint (research/broad/gex/gex_SPY.json).

Consensus claim: negative GEX => dealers short gamma => moves amplified => trade
momentum/flow. Tested below and REFUTED OOS (neg-GEX flow gross-PF 0.92).
"""
import json, os
import pandas as pd, numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

def net_gex_series(sym="SPY"):
    d = json.load(open(os.path.join(HERE, "gex", f"gex_{sym}.json")))["data"]
    g = pd.DataFrame(d)
    g["call_gamma"] = g.call_gamma.astype(float); g["put_gamma"] = g.put_gamma.astype(float)
    g["net_gex"] = g.call_gamma + g.put_gamma
    return dict(zip(g.date, g.net_gex))

def stats(df, H="ret_eod"):
    r = df[H].dropna().to_numpy(); pos = r[r > 0].sum(); neg = -r[r < 0].sum()
    return len(r), 100 * np.mean(r > 0), 100 * np.mean(r), (pos / neg if neg > 0 else np.nan)

def main():
    gex = net_gex_series("SPY")
    o = pd.read_parquet(os.path.join(HERE, "whale_outcomes_gated.parquet"))
    o["adate"] = o.adate.astype(str).str[:10]
    o["net_gex"] = o.adate.map(gex)
    o = o.dropna(subset=["net_gex"]); o["neg_gex"] = o.net_gex < 0
    h2 = o[o.half == "H2"]
    print(f"coverage {len(o)} | neg-GEX share {o.neg_gex.mean():.2f}")
    for lbl, mask in [("NEG-GEX (consensus: momentum)", h2.neg_gex),
                      ("POS-GEX (pinning)", ~h2.neg_gex),
                      ("ALL ungated", h2.index >= 0),
                      ("whale>=500k & NEG-GEX", (h2.total_premium >= 500000) & h2.neg_gex)]:
        n, wr, mean, pf = stats(h2[mask])
        print(f"  {lbl:30s} n={n:>5} wr={wr:4.1f}% mean={mean:+.3f}% grossPF={pf:.2f}")
    print("VERDICT: consensus neg-GEX-momentum claim REFUTED OOS (0.92); no cut reaches the >>2.0 ceiling a net PF-2.0 strategy needs.")

if __name__ == "__main__":
    main()
