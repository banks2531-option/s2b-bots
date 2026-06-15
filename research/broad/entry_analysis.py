"""Broad test, entry leg — derive interpretable entry-quality rules from the full
factor set, augmented with VIX term-structure, OOS-validated with a permutation
null over the whole rule search (the honest multiple-comparisons guard).

Data: ml_test/features.csv (246 mechanical SPY put-spread trades x 39 factors +
outcomes). Target = pnl_per_risk (payoff-aware). H1 = first 50% of dates (tune),
H2 = second 50% (test once).

Key question: does ANY single/pair factor rule, tuned on H1, beat the H2 baseline
out of sample by more than the search itself produces by chance?
"""
import os, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
FEAT = os.path.join(HERE, "..", "..", "ml_test", "features.csv")
RNG = np.random.default_rng(20260614)
N_PERM = 300
MIN_PASS = 40   # min H1 trades a rule must keep (power floor)

def add_term_structure(df):
    try:
        import yfinance as yf
        v = yf.download(["^VIX", "^VIX3M"], start="2025-01-01", end="2026-03-15", progress=False, auto_adjust=False)["Close"]
        v.index = pd.to_datetime(v.index).strftime("%Y-%m-%d")
        v = v.rename(columns={"^VIX": "vix", "^VIX3M": "vix3m"})
        v["term_slope"] = v["vix3m"] - v["vix"]            # >0 = contango (short-vol paid)
        v["contango"] = (v["term_slope"] > 0).astype(int)
        m = df["entry_date"].map(v["term_slope"]); c = df["entry_date"].map(v["contango"])
        df["term_slope"] = m.values; df["contango"] = c.values
        print(f"term-structure added: {df.term_slope.notna().sum()}/{len(df)} dates; contango share {df.contango.mean():.2f}")
    except Exception as e:
        print("term-structure unavailable:", e); df["term_slope"] = np.nan; df["contango"] = np.nan
    return df

def best_rule_for_factor(x, y, idx_h1):
    """Best (direction, threshold) on H1 maximizing H1 mean(y) among passing, >=MIN_PASS."""
    xh, yh = x[idx_h1], y[idx_h1]
    ok = ~np.isnan(xh)
    if ok.sum() < MIN_PASS: return None
    qs = np.unique(np.nanquantile(xh[ok], np.linspace(0.1, 0.9, 9)))
    best = None
    for thr in qs:
        for d in (1, -1):
            keep = (xh >= thr) if d == 1 else (xh <= thr)
            keep &= ok
            if keep.sum() >= MIN_PASS:
                m = yh[keep].mean()
                if best is None or m > best[2]:
                    best = (d, thr, m)
    return best  # (direction, threshold, h1_mean)

def search(X, y, factors, idx_h1, idx_h2):
    """Return per-factor frozen-rule H2 performance; statistic = max H2 mean among passing."""
    rows = []
    ybar_h2 = y[idx_h2].mean()
    for f in factors:
        x = X[f].to_numpy(float)
        br = best_rule_for_factor(x, y, idx_h1)
        if br is None: continue
        d, thr, h1m = br
        keep_h2 = ((x >= thr) if d == 1 else (x <= thr)) & ~np.isnan(x) & idx_h2_mask(idx_h2, len(x))
        n2 = keep_h2.sum()
        if n2 < 10: continue
        h2m = y[keep_h2].mean()
        rows.append({"factor": f, "dir": d, "thr": round(thr, 4), "h1_mean": h1m,
                     "h2_mean": h2m, "h2_lift": h2m - ybar_h2, "h2_n": int(n2),
                     "h2_winrate": float((y[keep_h2] > 0).mean())})
    return pd.DataFrame(rows).sort_values("h2_mean", ascending=False), ybar_h2

def idx_h2_mask(idx_h2, n):
    m = np.zeros(n, bool); m[idx_h2] = True; return m

def main():
    df = pd.read_csv(FEAT)
    df = add_term_structure(df)
    meta = {"entry_date", "exit_date", "exit_reason", "pnl", "max_loss", "credit", "spot", "win", "pnl_per_risk"}
    factors = [c for c in df.columns if c not in meta and df[c].dtype != object]
    y = df["pnl_per_risk"].to_numpy(float)
    n = len(df)
    cut = np.argsort(df["entry_date"].values)[: n // 2]
    order = np.argsort(df["entry_date"].values)
    idx_h1 = order[: n // 2]; idx_h2 = order[n // 2:]
    print(f"n={n}  H1={len(idx_h1)}  H2={len(idx_h2)}  factors={len(factors)}  H2 baseline pnl_per_risk={y[idx_h2].mean():.4f}")

    res, ybar = search(df, y, factors, idx_h1, idx_h2)
    print("\n=== top 12 single-factor rules by H2 mean pnl_per_risk (H1-tuned, H2-tested) ===")
    print(res.head(12).to_string(index=False))
    real_stat = res["h2_mean"].max()

    # permutation null over the WHOLE search (shuffle outcomes, keep factors)
    null = np.empty(N_PERM)
    for i in range(N_PERM):
        yp = RNG.permutation(y)
        r, _ = search(df, yp, factors, idx_h1, idx_h2)
        null[i] = r["h2_mean"].max() if len(r) else np.nan
    p = float(np.nanmean(null >= real_stat))
    print(f"\n=== permutation null (the honesty test) ===")
    print(f"real best H2 mean = {real_stat:.4f}  |  null 95th pct = {np.nanpercentile(null,95):.4f}  |  p = {p:.3f}")
    print("INTERPRETATION:", "a rule beats chance (p<0.05)" if p < 0.05 else "NO rule beats the search's own chance level -> overfit/noise")

    # full-model OOS sanity (logistic on win) for comparison to Test A
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        Xtr = df.iloc[idx_h1][factors]; Xte = df.iloc[idx_h2][factors]
        imp = SimpleImputer(strategy="median").fit(Xtr); sc = StandardScaler().fit(imp.transform(Xtr))
        clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=1000)
        clf.fit(sc.transform(imp.transform(Xtr)), df.iloc[idx_h1]["win"])
        pr = clf.predict_proba(sc.transform(imp.transform(Xte)))[:, 1]
        print(f"\nfull logistic OOS AUC (win): {roc_auc_score(df.iloc[idx_h2]['win'], pr):.3f}  (Test A got ~0.52; chance=0.50)")
    except Exception as e:
        print("model step skipped:", e)

    res.to_csv(os.path.join(HERE, "entry_rules.csv"), index=False)
    print("\n-> research/broad/entry_rules.csv")

if __name__ == "__main__":
    main()
