"""Test A core pipeline: walk-forward PURGED time-series CV of a multi-factor
ML model on S2b put-spread entries.

Protocol (strict):
  1. Expanding-window walk-forward ONLY (never random K-fold). Train on
     [start, t), predict the next STEP entries, roll. PURGE/EMBARGO: a training
     trade is dropped if its EXIT date >= the first test entry's entry_date
     (so no trade still open at the fold boundary leaks its label into train).
  2. Models: logistic regression (scaled) and HistGradientBoosting. Both fit
     per-fold on train only; OOS predictions concatenated across folds.
  3. Label-shuffle NULL: identical pipeline, labels permuted within train each
     fold (target leak / pipeline-leak detector).
  4. Also model continuous pnl_per_risk via HGB regressor for sizing.

Honesty instruments: OOS AUC + log-loss vs shuffled null; permutation feature
importance on OOS; explicit config count.

Outputs: oos_predictions.csv, ml_results.json
"""
import os
import json
import warnings
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import (HistGradientBoostingClassifier,
                              HistGradientBoostingRegressor)
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.metrics import roc_auc_score, log_loss

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
RNG = np.random.default_rng(20260613)

META = ["entry_date", "exit_date", "exit_reason", "pnl", "max_loss", "credit",
        "spot", "win", "pnl_per_risk"]


def load():
    df = pd.read_csv(os.path.join(HERE, "features.csv"))
    df = df.sort_values(["entry_date", "exit_date"]).reset_index(drop=True)
    feat_cols = [c for c in df.columns if c not in META]
    return df, feat_cols


def make_clf(kind):
    if kind == "logit":
        return Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, C=0.5,
                                       class_weight="balanced")),
        ])
    # HGB handles NaN natively; mild regularization for tiny n
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=200, learning_rate=0.05,
        min_samples_leaf=20, l2_regularization=1.0,
        early_stopping=False, random_state=0)


def walk_forward(df, feat_cols, kind, shuffle=False, init_frac=0.45, step=10):
    """Expanding window. Returns DataFrame of OOS rows with predicted prob.
    Purge: drop any train trade whose exit_date >= first test entry_date."""
    n = len(df)
    init = int(n * init_frac)
    preds = np.full(n, np.nan)
    fold_id = np.full(n, -1)
    X = df[feat_cols].values
    y = df["win"].values
    fold = 0
    t = init
    while t < n:
        test_idx = np.arange(t, min(t + step, n))
        first_test_entry = df.iloc[test_idx[0]]["entry_date"]
        # train = all trades before t, PURGED by exit date
        train_mask = np.zeros(n, dtype=bool)
        train_mask[:t] = True
        exit_dates = df["exit_date"].values
        purge = exit_dates >= first_test_entry  # still open at boundary
        train_mask = train_mask & ~purge
        tr = np.where(train_mask)[0]
        if len(tr) < 25 or len(np.unique(y[tr])) < 2:
            t += step
            continue
        ytr = y[tr].copy()
        if shuffle:
            ytr = RNG.permutation(ytr)
        model = make_clf(kind)
        model.fit(X[tr], ytr)
        p = model.predict_proba(X[test_idx])[:, 1]
        preds[test_idx] = p
        fold_id[test_idx] = fold
        fold += 1
        t += step
    out = df.copy()
    out["pred"] = preds
    out["fold"] = fold_id
    return out[~np.isnan(preds)].copy()


def walk_forward_reg(df, feat_cols, init_frac=0.45, step=10):
    """Same protocol, HGB regressor on pnl_per_risk -> predicted edge (for sizing)."""
    n = len(df)
    init = int(n * init_frac)
    preds = np.full(n, np.nan)
    X = df[feat_cols].values
    y = df["pnl_per_risk"].values
    t = init
    while t < n:
        test_idx = np.arange(t, min(t + step, n))
        first_test_entry = df.iloc[test_idx[0]]["entry_date"]
        train_mask = np.zeros(n, dtype=bool)
        train_mask[:t] = True
        purge = df["exit_date"].values >= first_test_entry
        tr = np.where(train_mask & ~purge)[0]
        if len(tr) < 25:
            t += step
            continue
        m = HistGradientBoostingRegressor(max_depth=3, max_iter=200,
                                          learning_rate=0.05,
                                          min_samples_leaf=20,
                                          l2_regularization=1.0, random_state=0)
        m.fit(X[tr], y[tr])
        preds[test_idx] = m.predict(X[test_idx])
        t += step
    out = df.copy()
    out["pred_edge"] = preds
    return out[~np.isnan(preds)].copy()


def metrics(d):
    y, p = d["win"].values, d["pred"].values
    try:
        auc = roc_auc_score(y, p)
    except ValueError:
        auc = float("nan")
    ll = log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1])
    return {"n": int(len(d)), "auc": round(float(auc), 4),
            "log_loss": round(float(ll), 4), "base_rate": round(float(y.mean()), 4)}


def perm_importance(df, feat_cols, kind="hgb", n_rep=15):
    """OOS permutation importance: refit full WF, shuffle one feature's column
    in the TEST block each fold, measure AUC drop. Cheap proxy on concatenated
    OOS preds via a single train/test on the last expanding split."""
    # Use a holdout: train on first 60%, score last 40%, permute features.
    df = df.reset_index(drop=True)
    n = len(df); cut = int(n * 0.6)
    tr, te = np.arange(cut), np.arange(cut, n)
    X = df[feat_cols].values; y = df["win"].values
    model = make_clf(kind)
    model.fit(X[tr], y[tr])
    base = roc_auc_score(y[te], model.predict_proba(X[te])[:, 1])
    imp = {}
    Xte = X[te].copy()
    for j, c in enumerate(feat_cols):
        drops = []
        for _ in range(n_rep):
            Xp = Xte.copy()
            Xp[:, j] = RNG.permutation(Xp[:, j])
            a = roc_auc_score(y[te], model.predict_proba(Xp)[:, 1])
            drops.append(base - a)
        imp[c] = round(float(np.mean(drops)), 4)
    return base, dict(sorted(imp.items(), key=lambda kv: -kv[1]))


def main():
    df, feat_cols = load()
    results = {"n_obs": len(df), "n_features": len(feat_cols),
               "feat_cols": feat_cols, "win_base_rate": round(float(df["win"].mean()), 4),
               "configs_tried": []}

    real, null = {}, {}
    oos_store = {}
    for kind in ("logit", "hgb"):
        rd = walk_forward(df, feat_cols, kind, shuffle=False)
        nd = walk_forward(df, feat_cols, kind, shuffle=True)
        real[kind] = metrics(rd)
        null[kind] = metrics(nd)
        oos_store[kind] = rd
        results["configs_tried"].append(f"{kind}_classifier_default")

    results["oos_real"] = real
    results["oos_shuffled_null"] = null

    # regressor for sizing
    reg = walk_forward_reg(df, feat_cols)
    # corr of predicted edge vs realized pnl_per_risk (OOS)
    rr = float(np.corrcoef(reg["pred_edge"], reg["pnl_per_risk"])[0, 1])
    results["regressor_oos_corr_pred_vs_realized"] = round(rr, 4)
    reg[["entry_date", "exit_date", "pnl", "pnl_per_risk", "max_loss",
         "win", "pred_edge"]].to_csv(os.path.join(HERE, "oos_reg.csv"), index=False)

    # store HGB classifier OOS preds (used by backtest)
    oos_store["hgb"][["entry_date", "exit_date", "exit_reason", "pnl",
                      "max_loss", "win", "pred", "fold"]].to_csv(
        os.path.join(HERE, "oos_predictions.csv"), index=False)
    # also logit
    oos_store["logit"][["entry_date", "exit_date", "pnl", "max_loss", "win",
                        "pred"]].to_csv(os.path.join(HERE, "oos_pred_logit.csv"),
                                        index=False)

    base_auc, imp = perm_importance(df, feat_cols, "hgb")
    results["perm_importance_holdout_auc"] = round(float(base_auc), 4)
    results["perm_importance_top"] = dict(list(imp.items())[:15])

    with open(os.path.join(HERE, "ml_results.json"), "w") as f:
        json.dump(results, f, indent=2)

    print(json.dumps({k: results[k] for k in
                      ["n_obs", "n_features", "win_base_rate", "oos_real",
                       "oos_shuffled_null", "regressor_oos_corr_pred_vs_realized",
                       "perm_importance_holdout_auc"]}, indent=2))
    print("\nTop permutation importances (AUC drop, holdout):")
    for k, v in list(imp.items())[:15]:
        print(f"  {v:+.4f}  {k}")


if __name__ == "__main__":
    main()
