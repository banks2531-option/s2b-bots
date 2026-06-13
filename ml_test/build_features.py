"""Build the ML feature matrix for Test A.

Unit of observation = each S2b-style daily put-spread entry in the existing
246-trade daily panel (2025-03-03 -> 2026-02-27). Labels:
  - win  : 1 if pnl > 0 else 0  (binary)
  - pnl_per_risk : pnl / max_loss_per_contract  (continuous, P&L per $ risked)

CARDINAL RULE: every feature is point-in-time as of the 10:00 ET entry. No
lookahead. Concretely:
  - SPY-daily technicals use ONLY bars with date < entry_date (prior close and
    earlier). The entry-day high/low/close are NOT known at 10:00.
  - The entry-day OPEN and the 10:00 spot ARE known -> gap = open/prevclose-1,
    intraday_to_10 = spot/open-1 are admissible.
  - VIX uses prior-day close + prior change (intraday VIX not in daily bars).
  - Flow features (flow_features.csv) are AM 04:00-09:59 ET by construction.
  - Recent-performance features use ONLY trades that EXITED strictly before the
    current entry_date (purged; no open/overlapping trade leaks its outcome).

Outputs: features.csv (one row per entry, all features + both labels + meta).
"""
import os
import math
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.abspath(os.path.join(HERE, "..", "simulations"))

WIDTH = 10.0  # S2b wing width in points; max loss/contract = (WIDTH - credit)*100


# ----------------------------------------------------------------- load panel
def load_panel(fname):
    df = pd.read_csv(os.path.join(SIM, fname))
    df = df.sort_values(["entry_date", "exit_date"]).reset_index(drop=True)
    # short-strike delta (abs) from "put_short=-0.409"
    df["short_delta"] = (df["deltas"].str.extract(r"put_short=(-?[0-9.]+)")
                         .astype(float).abs())
    df["credit"] = pd.to_numeric(df["credit"], errors="coerce")
    df["max_loss"] = (WIDTH - df["credit"]) * 100.0
    df["win"] = (df["pnl"] > 0).astype(int)
    df["pnl_per_risk"] = df["pnl"] / df["max_loss"]
    df["credit_width"] = df["credit"] / WIDTH
    return df


# ----------------------------------------------- SPY daily point-in-time tech
def build_spy_features():
    spy = pd.read_csv(os.path.join(HERE, "spy_daily.csv"))
    spy = spy.sort_values("date").reset_index(drop=True)
    c = spy["spy_close"]
    h, l = spy["spy_high"], spy["spy_low"]
    pc = c.shift(1)

    # trailing returns / SMAs computed on CLOSE; all use data up to that row's
    # close. We will look these up at the PRIOR session (< entry_date).
    spy["ret_1d"] = c / c.shift(1) - 1
    spy["ret_5d"] = c / c.shift(5) - 1
    spy["ret_10d"] = c / c.shift(10) - 1
    spy["ret_20d"] = c / c.shift(20) - 1
    spy["sma20"] = c.rolling(20).mean()
    spy["sma50"] = c.rolling(50).mean()
    spy["dist_sma20"] = c / spy["sma20"] - 1
    spy["dist_sma50"] = c / spy["sma50"] - 1
    spy["above_sma20"] = (c > spy["sma20"]).astype(int)
    spy["above_sma50"] = (c > spy["sma50"]).astype(int)
    # realized vol (annualized) of daily log returns
    lr = np.log(c / c.shift(1))
    for w in (5, 10, 21):
        spy[f"rv{w}"] = lr.rolling(w).std() * math.sqrt(252) * 100
    # RSI(14) Wilder
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    al = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = ag / al
    spy["rsi14"] = 100 - 100 / (1 + rs)
    # ATR(14) as % of close
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    spy["atr14_pct"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / c * 100
    # distance from trailing 20d high (of closes)
    spy["dist_hi20"] = c / c.rolling(20).max() - 1
    # vix level + change (prior close based)
    spy["vix_chg"] = spy["vix_close"] - spy["vix_close"].shift(1)

    spy["open_today"] = spy["spy_open"]
    spy["prev_close"] = pc
    return spy


# --------------------------------------------------- merge prior-session tech
def attach_technicals(panel, spy):
    spy_dates = spy["date"].tolist()
    spy_idx = {d: i for i, d in enumerate(spy_dates)}
    tech_cols = ["ret_1d", "ret_5d", "ret_10d", "ret_20d", "dist_sma20",
                 "dist_sma50", "above_sma20", "above_sma50", "rv5", "rv10",
                 "rv21", "rsi14", "atr14_pct", "dist_hi20", "vix_close",
                 "vix_chg"]
    rows = []
    for _, t in panel.iterrows():
        ed = t["entry_date"]
        # find the last spy session strictly BEFORE entry_date (prior close)
        prior_i = None
        for i, d in enumerate(spy_dates):
            if d < ed:
                prior_i = i
            else:
                break
        rec = {}
        if prior_i is None:
            for c in tech_cols:
                rec[c] = np.nan
            rec["gap"] = np.nan
            rec["intraday_to_10"] = np.nan
        else:
            prow = spy.iloc[prior_i]
            for c in tech_cols:
                rec[c] = prow[c]
            # entry-day open is known at 10:00; spot is the 10:00 spot.
            # find entry-day's own spy row (for the open only) if present
            ei = spy_idx.get(ed)
            if ei is not None:
                open_today = spy.iloc[ei]["spy_open"]
                rec["gap"] = open_today / prow["spy_close"] - 1
                rec["intraday_to_10"] = (t["spot"] / open_today - 1
                                         if open_today else np.nan)
            else:
                # fall back: gap from 10:00 spot vs prior close
                rec["gap"] = t["spot"] / prow["spy_close"] - 1
                rec["intraday_to_10"] = np.nan
        rows.append(rec)
    return pd.concat([panel.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


# ------------------------------------------------------ flow + regime + ivrv
def attach_flow_regime_iv(df):
    flow = pd.read_csv(os.path.join(SIM, "flow_features.csv"))
    flow_cols = ["idx_put_share_am", "idx_put_ask_prem_am",
                 "idx_put_sweep_cnt_am", "breadth_bear_am", "dp_prem_am",
                 "idx_put_share_prior", "idx_put_ask_prem_prior", "dvix",
                 "alert_cnt_am"]  # dp_idx_sell_share_am dropped (all-NaN)
    df = df.merge(flow[["date"] + flow_cols], left_on="entry_date",
                  right_on="date", how="left").drop(columns="date")

    reg = pd.read_csv(os.path.join(SIM, "regime_series.csv"))
    # regime_series is keyed to a date; use the entry-day label if present,
    # else the most recent prior label (point-in-time: regime is computed from
    # prior-close SMA state per the regime study, so same-day label is PIT).
    reg = reg.sort_values("date").reset_index(drop=True)
    reg_map = dict(zip(reg["date"], reg["regime"]))
    reg_dates = reg["date"].tolist()

    def reg_lookup(ed):
        if ed in reg_map:
            return reg_map[ed]
        prior = [d for d in reg_dates if d <= ed]
        return reg_map[prior[-1]] if prior else None
    df["regime"] = df["entry_date"].map(reg_lookup)
    df["regime_up"] = (df["regime"] == "UP").astype(int)
    df["regime_down"] = (df["regime"] == "DOWN").astype(int)

    # IV / IV-minus-RV from ivrv_series: use PRIOR session row (PIT at 10:00)
    iv = pd.read_csv(os.path.join(SIM, "ivrv_series.csv")).sort_values("date")
    iv_dates = iv["date"].tolist()
    iv_map = iv.set_index("date").to_dict("index")

    def iv_prior(ed, col):
        prior = [d for d in iv_dates if d < ed]
        if not prior:
            return np.nan
        v = iv_map[prior[-1]].get(col)
        return v if v is not None and not (isinstance(v, float) and math.isnan(v)) else np.nan
    for col, out in [("iv_alert_spy", "iv_prior"),
                     ("vix_minus_rv", "iv_minus_rv_prior"),
                     ("ivalert_minus_rv", "ivalert_minus_rv_prior")]:
        df[out] = df["entry_date"].apply(lambda ed, c=col: iv_prior(ed, c))
    return df


# ------------------------------------------ recent-performance (PURGED) feats
def attach_recent_perf(df, N=10):
    """Rolling stats over the strategy's own trailing trades that have already
    EXITED strictly before the current entry_date. Purged: an open trade whose
    outcome is not yet realized at the current entry cannot contribute."""
    df = df.sort_values(["entry_date", "exit_date"]).reset_index(drop=True)
    wr, avg_pnl, last_pnl, cum_dd = [], [], [], []
    for _, t in df.iterrows():
        ed = t["entry_date"]
        done = df[df["exit_date"] < ed]  # strictly-prior exits only
        if len(done) == 0:
            wr.append(np.nan); avg_pnl.append(np.nan)
            last_pnl.append(np.nan); cum_dd.append(np.nan)
            continue
        tail = done.tail(N)
        wr.append(tail["win"].mean())
        avg_pnl.append(tail["pnl_per_risk"].mean())
        last_pnl.append(done.iloc[-1]["pnl_per_risk"])
        # trailing equity drawdown over all prior exits (per-risk units)
        eq = done["pnl_per_risk"].cumsum().values
        peak = np.maximum.accumulate(eq)
        cum_dd.append(float((peak - eq)[-1]))
    df[f"recent_wr{N}"] = wr
    df[f"recent_avgpnl{N}"] = avg_pnl
    df["recent_last_pnl"] = last_pnl
    df["recent_dd"] = cum_dd
    return df


def main():
    panel = load_panel("trades_panel_daily.csv")
    spy = build_spy_features()
    panel = attach_technicals(panel, spy)
    panel = attach_flow_regime_iv(panel)
    panel = attach_recent_perf(panel, N=10)

    # microstructure already present: credit_width, short_delta, dte
    panel["dte_f"] = panel["dte"].astype(float)

    keep_meta = ["entry_date", "exit_date", "exit_reason", "pnl", "max_loss",
                 "credit", "spot", "win", "pnl_per_risk"]
    feat_cols = [
        # flow (9)
        "idx_put_share_am", "idx_put_ask_prem_am", "idx_put_sweep_cnt_am",
        "breadth_bear_am", "dp_prem_am", "idx_put_share_prior",
        "idx_put_ask_prem_prior", "dvix", "alert_cnt_am",
        # regime
        "regime_up", "regime_down", "above_sma20", "above_sma50",
        "dist_sma20", "dist_sma50",
        # technicals/trend
        "ret_1d", "ret_5d", "ret_10d", "ret_20d", "rsi14", "atr14_pct",
        "rv5", "rv10", "rv21", "gap", "intraday_to_10", "dist_hi20",
        # vol/iv
        "vix_close", "vix_chg", "iv_prior", "iv_minus_rv_prior",
        "ivalert_minus_rv_prior",
        # microstructure
        "credit_width", "short_delta", "dte_f",
        # recent performance (purged)
        "recent_wr10", "recent_avgpnl10", "recent_last_pnl", "recent_dd",
    ]
    out = panel[keep_meta + feat_cols].copy()
    out.to_csv(os.path.join(HERE, "features.csv"), index=False)

    print(f"rows: {len(out)}  features: {len(feat_cols)}")
    print(f"win rate: {out['win'].mean():.3f}  "
          f"mean pnl_per_risk: {out['pnl_per_risk'].mean():.4f}")
    print("\nNaN counts per feature (early-window warmup expected):")
    nc = out[feat_cols].isna().sum()
    print(nc[nc > 0].to_string() if (nc > 0).any() else "  none")
    print("\nfeature list:")
    for c in feat_cols:
        print(" ", c)


if __name__ == "__main__":
    main()
