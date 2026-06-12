"""Post-run diagnostics: (1) spot-check one F1 and one F2 trade against raw
1-min bars; (2) gross-of-cost decomposition (points captured per trade before
commission/slippage) to separate 'no edge' from 'edge eaten by costs'."""
import pandas as pd
import numpy as np

OUTDIR = r"C:\Users\FixUser123\Documents\fable options\futures_validation"
bars = pd.read_parquet(f"{OUTDIR}\\es_1m.parquet")

def show_trade(csv, idx):
    t = pd.read_csv(f"{OUTDIR}\\{csv}").iloc[idx]
    print(f"\n--- {csv} #{idx}: {dict(t)}")
    d = bars[bars.et_date == t.date]
    em, xm = int(t.entry_min), int(t.exit_min)
    win = d[(d.et_time.str[:2].astype(int)*60 + d.et_time.str[3:].astype(int)).between(em-12, xm+2)]
    print(win[["et_time","open","high","low","close"]].to_string(index=False))

show_trade("trades_f1_midpoint.csv", 0)
show_trade("trades_f2_orb_long_up.csv", 0)

print("\n\n=== COST DECOMPOSITION (baseline runs) ===")
print(f"{'run':24s} {'n':>4} {'gross_pts/trade':>15} {'gross_$':>9} {'net_$':>9} {'cost_$':>8} {'gross_PF':>8}")
for name in ["f1_midpoint", "f2_orb_long_up", "f3_trend_t12"]:
    df = pd.read_csv(f"{OUTDIR}\\trades_{name}.csv")
    # net pnl already includes comm+slip in fills. Reconstruct gross points from
    # entry/exit fills is net of slippage; instead rebuild from raw direction move:
    # gross = pnl + commission; slippage is embedded in fills, estimate it back:
    comm = 2.48
    df["pnl_plus_comm"] = df.pnl + comm
    # slippage estimate: entry always 1 tick=1.25; stop exits 2 ticks=2.50; eod 1.25; tp 0
    slip = {"stop": 2.50 + 1.25, "take_profit": 1.25, "eod_flatten": 2.50}
    df["slip_est"] = df.reason.map(slip)
    df["gross"] = df.pnl_plus_comm + df.slip_est
    gp = df.loc[df.gross > 0, "gross"].sum(); gl = -df.loc[df.gross < 0, "gross"].sum()
    print(f"{name:24s} {len(df):>4} {df.gross.mean()/5:>15.2f} {df.gross.sum():>9.0f} "
          f"{df.pnl.sum():>9.0f} {(df.gross-df.pnl).sum():>8.0f} {gp/gl:>8.2f}")

print("\n=== per-setup / direction detail ===")
for name in ["f1_midpoint", "f2_orb_long_up", "f3_trend_t12"]:
    df = pd.read_csv(f"{OUTDIR}\\trades_{name}.csv")
    for (s, dr), g in df.groupby(["setup", "dir"]):
        gp = g.loc[g.pnl>0,"pnl"].sum(); gl = -g.loc[g.pnl<0,"pnl"].sum()
        pf = gp/gl if gl else float("inf")
        print(f"  {name:20s} {s:20s} {dr:5s} n={len(g):4d} WR={100*(g.pnl>0).mean():5.1f}% "
              f"PF={pf:5.2f} pnl={g.pnl.sum():9.2f}")
    print("  exit reasons:", df.reason.value_counts().to_dict())
