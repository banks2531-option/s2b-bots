"""Intraday entry-throttle test for the all-days S2b bot (2026-07-02).

Question (from the 7/2 sell-off): does SKIPPING entries on weak/vol-spiky days beat trading every day,
NET of the premium forgone? Or does throttling just cut volume without selecting losers?

Data = the REAL all-days S2b trade ledger from the Polygon-intraday engine
(`simulations/trades_panel_daily.csv`, 246 trades, 2025-03..2026-02, all weekdays), plus its 2x-slippage
twin for cost stress. Each trade carries `spot` = SPY AT ENTRY (10:00 ET) and `sigma` = entry IV — both
known at entry, so throttles built on them are look-ahead-free.

Throttles tested (all observable at entry):
  gapdown(g): skip if SPY at entry is down > g vs prior close (morning weakness / gap-down)
  volhigh(v): skip if entry IV (sigma) >= v (selling into a vol spike)
  trendoff : skip if SPY(entry) < its 200d MA  (the already-validated Phase-1 gate, for reference)

Discipline: baseline vs throttled PF & total P&L; did we skip LOSERS or just trades?; H1(tune)/H2(test)
split by entry date; a RANDOM-SKIP NULL (skip the same N random trades, 5000 draws — does the signal
select losing entries better than chance?); and the same on the 2x-cost ledger."""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, yfinance as yf

LEDGER = "simulations/trades_panel_daily.csv"
LEDGER_2X = "simulations/trades_panel_daily_slip2x.csv"


def load_ledger(path):
    df = pd.read_csv(path)
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    return df.sort_values("entry_date").reset_index(drop=True)


def attach_signals(df):
    """Join look-ahead-free entry-day context on the SPY ETF (same scale as the ledger `spot`):
    prior-day close (for the entry-time move) and the 200d MA (through yesterday)."""
    lo = (df["entry_date"].min() - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    hi = (df["entry_date"].max() + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    spy = yf.download("SPY", start=lo, end=hi, progress=False, auto_adjust=False)["Close"]
    spy = (spy.iloc[:, 0] if hasattr(spy, "columns") else spy).dropna()
    prior_close = spy.shift(1)                 # close known BEFORE the entry day (no look-ahead)
    sma200 = spy.rolling(200).mean().shift(1)  # 200d MA through yesterday (no look-ahead)
    idx = {d.date(): i for i, d in enumerate(spy.index)}
    rows = []
    for _, r in df.iterrows():
        i = idx.get(r["entry_date"].date())
        rows.append({"prior_close": prior_close.iloc[i] if i is not None else np.nan,
                     "ma200": sma200.iloc[i] if i is not None else np.nan})
    return pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def pf(pnl):
    w = pnl[pnl > 0].sum(); l = -pnl[pnl <= 0].sum()
    return (w / l) if l > 0 else float("inf")


def summarize(name, pnl):
    return "%-22s n=%3d  PF=%4.2f  total=$%+8.0f  win%%=%2.0f  avg/trade=$%+6.1f" % (
        name, len(pnl), pf(pnl), pnl.sum(), (pnl > 0).mean() * 100, pnl.mean())


def run(path, label):
    df = attach_signals(load_ledger(path))
    # ledger `spot` = SPY ETF at the 10:00 ET entry; prior_close = SPY ETF prior-day close. Both same scale.
    df["entry_move"] = df["spot"] / df["prior_close"] - 1.0   # SPY move from prior close to the 10:00 entry
    df["below_ma"] = df["spot"] < df["ma200"]                 # SPY at entry below its 200d MA
    pnl = df["pnl"]

    print("\n===== %s =====" % label)
    print(summarize("BASELINE (all-days)", pnl))
    print("  by weekday PF:", {int(k): round(pf(g["pnl"]), 2) for k, g in df.groupby(df["entry_date"].dt.weekday)})

    throttles = {
        "gapdown>0.30%": df["entry_move"] <= -0.003,
        "gapdown>0.50%": df["entry_move"] <= -0.005,
        "gapdown>0.75%": df["entry_move"] <= -0.0075,
        "volhigh IV>=0.25": df["sigma"] >= 0.25,
        "volhigh IV>=0.28": df["sigma"] >= 0.28,
        "trendoff(<200dMA)": df["below_ma"],
    }
    print("  -- throttled books (skip = the flagged entries) --")
    for name, mask in throttles.items():
        mask = mask.fillna(False).values
        kept, skipped = pnl[~mask], pnl[mask]
        if mask.sum() == 0:
            print("  %-20s (no entries flagged)" % name); continue
        # NULL: skip the same N at random 5000x; does the signal remove MORE loss than chance?
        rng = np.random.default_rng(7); K = int(mask.sum()); v = pnl.values; n = len(v)
        rand_removed = np.array([v[rng.choice(n, K, replace=False)].sum() for _ in range(5000)])
        p = (rand_removed <= skipped.sum()).mean()   # p(random removes >= this much loss)
        print("  %-20s skip=%2d  KEPT PF=%4.2f $%+7.0f (avg $%+5.1f) | skipped $%+6.0f (avg $%+6.1f)  null p=%.3f" % (
            name, K, pf(kept), kept.sum(), kept.mean(), skipped.sum(), skipped.mean(), p))

    # H1 (tune) / H2 (test) split by entry date
    mid = df["entry_date"].quantile(0.5)
    print("  -- OOS split (H1 tune / H2 test), best gapdown & volhigh --")
    for name, mask in [("gapdown>0.50%", df["entry_move"] <= -0.005), ("volhigh IV>=0.25", df["sigma"] >= 0.25)]:
        mask = mask.fillna(False)
        for tag, sub in [("H1", df["entry_date"] <= mid), ("H2", df["entry_date"] > mid)]:
            b = df.loc[sub, "pnl"]; k = df.loc[sub & ~mask, "pnl"]
            print("    %-16s %s  baseline PF %4.2f $%+7.0f  ->  throttled PF %4.2f $%+7.0f (skipped %d)"
                  % (name, tag, pf(b), b.sum(), pf(k), k.sum(), int((sub & mask).sum())))


run(LEDGER, "REAL all-days S2b ledger (base slippage)")
run(LEDGER_2X, "REAL all-days S2b ledger (2x slippage stress)")
