"""Decision-rule backtest: use the model's OOS win-probability to FILTER
(take/skip) or SIZE the mechanical S2b trades. Threshold chosen on TRAIN folds
only, frozen, applied OOS. Compare resulting OOS PF / P&L / maxDD vs
unconditioned S2b on the SAME OOS trade set.

Two things to be careful about:
  - The unconditioned daily-entry panel is itself NOT the tradable S2b arm
    (panel PF ~1.03; the real S2b edge is Monday-only, PF 1.86). The ML filter
    is being tested on the same daily panel, so the apples-to-apples baseline
    is the *unconditioned panel restricted to the OOS window*, NOT the 1.86
    Monday number. We report the ML filter vs that matched baseline, then state
    plainly whether ML could even rescue the (weaker) daily panel.
  - Threshold is picked on each fold's TRAIN labels (maximize train PF subject
    to keeping >=50% of trades), then frozen and applied to that fold's OOS
    block. Concatenate OOS. This is the honest, leak-free version of the rule.
"""
import os
import json
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
H1_END = "2025-08-28"
META = ["entry_date", "exit_date", "exit_reason", "pnl", "max_loss", "credit",
        "spot", "win", "pnl_per_risk"]


def load():
    df = pd.read_csv(os.path.join(HERE, "features.csv"))
    return df.sort_values(["entry_date", "exit_date"]).reset_index(drop=True)


def make_clf(kind):
    if kind == "logit":
        return Pipeline([("imp", SimpleImputer(strategy="median")),
                         ("sc", StandardScaler()),
                         ("clf", LogisticRegression(max_iter=2000, C=0.5,
                                                    class_weight="balanced"))])
    return HistGradientBoostingClassifier(max_depth=3, max_iter=200,
                                          learning_rate=0.05, min_samples_leaf=20,
                                          l2_regularization=1.0,
                                          early_stopping=False, random_state=0)


def pf(pnls):
    pnls = np.asarray(pnls)
    gw = pnls[pnls > 0].sum()
    gl = -pnls[pnls <= 0].sum()
    return gw / gl if gl > 0 else (np.inf if gw > 0 else 0.0)


def maxdd(pnls):
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    return float((peak - eq).max()) if len(eq) else 0.0


def stats(pnls):
    pnls = np.asarray(pnls)
    return {"n": int(len(pnls)),
            "pf": round(float(pf(pnls)), 3) if np.isfinite(pf(pnls)) else None,
            "pnl": round(float(pnls.sum()), 1),
            "maxdd": round(maxdd(pnls), 1),
            "wr": round(float((pnls > 0).mean()), 3) if len(pnls) else None}


def best_threshold_train(p_train, y_train, pnl_train, min_keep=0.5):
    """Pick prob threshold maximizing TRAIN PF, keeping >= min_keep fraction.
    Grid over candidate thresholds = the train prob quantiles."""
    cand = np.quantile(p_train, np.linspace(0.0, 0.5, 26))  # never cut >50%
    best_thr, best_pf = -np.inf, -np.inf
    for thr in cand:
        keep = p_train >= thr
        if keep.mean() < min_keep or keep.sum() < 10:
            continue
        f = pf(pnl_train[keep])
        if f > best_pf:
            best_pf, best_thr = f, thr
    if best_thr == -np.inf:
        best_thr = np.min(p_train)  # keep all
    return best_thr


def walk_forward_filter(df, kind, costs="base", init_frac=0.45, step=10):
    """Returns per-trade OOS records with: pnl (at chosen cost), model prob,
    train-frozen threshold, take/skip decision, predicted edge for sizing."""
    feat_cols = [c for c in df.columns if c not in META]
    n = len(df)
    X = df[feat_cols].values
    y = df["win"].values
    # cost handling: panel pnl is at base; for 2x we load slip2x panel pnls
    pnl = df["pnl"].values.astype(float)
    if costs == "slip2x":
        s2 = pd.read_csv(os.path.join(HERE, "..", "simulations",
                                      "trades_panel_daily_slip2x.csv"))
        s2 = s2.sort_values(["entry_date", "exit_date"]).reset_index(drop=True)
        # align by (entry_date, exit_date)
        key = df[["entry_date", "exit_date"]].apply(tuple, axis=1)
        s2map = {(r.entry_date, r.exit_date): r.pnl for r in s2.itertuples()}
        pnl = np.array([s2map.get(k, np.nan) for k in key])
    rows = []
    init = int(n * init_frac)
    t = init
    while t < n:
        test_idx = np.arange(t, min(t + step, n))
        first_test_entry = df.iloc[test_idx[0]]["entry_date"]
        train_mask = np.zeros(n, dtype=bool); train_mask[:t] = True
        purge = df["exit_date"].values >= first_test_entry
        tr = np.where(train_mask & ~purge)[0]
        if len(tr) < 25 or len(np.unique(y[tr])) < 2:
            t += step; continue
        model = make_clf(kind)
        model.fit(X[tr], y[tr])
        p_tr = model.predict_proba(X[tr])[:, 1]
        thr = best_threshold_train(p_tr, y[tr], df["pnl"].values[tr])
        p_te = model.predict_proba(X[test_idx])[:, 1]
        for j, idx in enumerate(test_idx):
            rows.append({"entry_date": df.iloc[idx]["entry_date"],
                         "exit_date": df.iloc[idx]["exit_date"],
                         "pnl": pnl[idx], "prob": p_te[j], "thr": thr,
                         "take": int(p_te[j] >= thr)})
        t += step
    return pd.DataFrame(rows)


def halves(d):
    return d[d["entry_date"] <= H1_END], d[d["entry_date"] > H1_END]


def backtest(df):
    out = {}
    for costs in ("base", "slip2x"):
        cres = {}
        for kind in ("logit", "hgb"):
            r = walk_forward_filter(df, kind, costs=costs)
            r = r.dropna(subset=["pnl"])
            uncond = r["pnl"].values
            taken = r[r["take"] == 1]["pnl"].values
            h1, h2 = halves(r)
            h1t, h2t = h1[h1["take"] == 1], h2[h2["take"] == 1]
            cres[kind] = {
                "n_oos": int(len(r)), "n_taken": int(r["take"].sum()),
                "uncond_oos": stats(uncond),
                "uncond_h1": stats(halves(r)[0]["pnl"].values),
                "uncond_h2": stats(halves(r)[1]["pnl"].values),
                "filter_oos": stats(taken),
                "filter_h1": stats(h1t["pnl"].values),
                "filter_h2": stats(h2t["pnl"].values),
            }
        out[costs] = cres
    return out


def main():
    df = load()
    res = backtest(df)
    with open(os.path.join(HERE, "backtest_results.json"), "w") as f:
        json.dump(res, f, indent=2)
    for costs, cres in res.items():
        print(f"\n===== COSTS={costs} =====")
        for kind, r in cres.items():
            print(f"\n--- {kind} ---")
            print(f"  OOS n={r['n_oos']}  filter takes {r['n_taken']}")
            u, ff = r["uncond_oos"], r["filter_oos"]
            print(f"  UNCOND  OOS  PF {u['pf']}  P&L {u['pnl']}  DD {u['maxdd']}  "
                  f"WR {u['wr']}  (H1 PF {r['uncond_h1']['pf']} / H2 PF {r['uncond_h2']['pf']})")
            print(f"  FILTER  OOS  PF {ff['pf']}  P&L {ff['pnl']}  DD {ff['maxdd']}  "
                  f"WR {ff['wr']}  (H1 PF {r['filter_h1']['pf']} / H2 PF {r['filter_h2']['pf']})")


if __name__ == "__main__":
    main()
