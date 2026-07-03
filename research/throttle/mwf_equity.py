"""Test the data-supported lever: DROP Tue/Thu (trade Mon/Wed/Fri) vs all-days vs Monday-only,
simulated as a real compounding account at $5k and $20k starting balances (2026-07-02).

Uses the REAL all-days S2b ledger (`simulations/trades_panel_daily.csv`, per-contract P&L, 2025-03..
2026-02) + its 2x-slippage twin. Sizing/concurrency mirror Bot B: risk 10%/trade (min 1 contract, the
bot's floor), max 5 concurrent, total open risk <= 50% of equity; entries that can't fit are skipped.
Event-driven so overlapping positions and the total-risk cap are modeled; balance compounds on each exit.
Per-contract P&L is scaled linearly by qty (a fills-don't-move approximation on liquid SPY; the 2x ledger
stresses fills)."""
import warnings; warnings.filterwarnings("ignore")
import re, math
import numpy as np, pandas as pd

RISK_PCT, MAX_OPEN, TOTAL_CAP = 0.10, 5, 0.50


def wing_of(legs):
    n = [float(x) for x in re.findall(r"[-+]?\d+\.?\d*", str(legs))]
    return abs(n[0] - n[1]) if len(n) >= 2 else 10.0


def load(path):
    df = pd.read_csv(path)
    df["wd"] = pd.to_datetime(df["entry_date"]).dt.weekday
    df["wing"] = df["legs"].map(wing_of)
    df["max_loss_pc"] = ((df["wing"] - df["credit"]) * 100).clip(lower=1.0)
    df["ent"] = pd.to_datetime(df["entry_date"] + " " + df["entry_time_et"].astype(str))
    df["ext"] = pd.to_datetime(df["exit_date"] + " " + df["exit_time_et"].astype(str))
    return df.sort_values("ent").reset_index(drop=True)


def simulate(df, start_bal):
    """Event-driven compounding sim with sizing + concurrency + total-risk cap."""
    bal = start_bal
    open_pos = []                    # list of dicts: {ext, qty, risk, pnl_pc}
    eq_curve = [start_bal]
    taken = skipped = 0
    realized = []                    # per-trade realized P&L (for PF)
    # build a merged timeline: process each entry, releasing exits that occurred before it
    events = []
    for i, r in df.iterrows():
        events.append((r["ent"], "entry", i))
    events.sort(key=lambda e: e[0])
    for ts, _, i in events:
        r = df.iloc[i]
        # release all positions that exited at or before this entry (free capacity, compound)
        still = []
        for p in open_pos:
            if p["ext"] <= ts:
                pnl = p["pnl_pc"] * p["qty"]
                bal += pnl; realized.append(pnl); eq_curve.append(bal)
            else:
                still.append(p)
        open_pos = still
        open_risk = sum(p["risk"] for p in open_pos)
        mlpc = r["max_loss_pc"]
        # can we fit at least one contract under concurrency + total-risk cap?
        if len(open_pos) >= MAX_OPEN or (open_risk + mlpc) > TOTAL_CAP * bal:
            skipped += 1; continue
        qty_target = max(1, math.floor(RISK_PCT * bal / mlpc))
        qty_room = math.floor((TOTAL_CAP * bal - open_risk) / mlpc)
        qty = max(1, min(qty_target, qty_room))
        open_pos.append({"ext": r["ext"], "qty": qty, "risk": qty * mlpc, "pnl_pc": r["pnl"]})
        taken += 1
    # close any still-open at the end
    for p in sorted(open_pos, key=lambda p: p["ext"]):
        pnl = p["pnl_pc"] * p["qty"]; bal += pnl; realized.append(pnl); eq_curve.append(bal)
    eq = np.array(eq_curve)
    maxdd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    realized = np.array(realized)
    gw = realized[realized > 0].sum(); gl = -realized[realized <= 0].sum()
    pf = gw / gl if gl > 0 else float("inf")
    span_yrs = (df["ext"].max() - df["ent"].min()).days / 365.25
    ret = bal / start_bal - 1
    cagr = (bal / start_bal) ** (1 / span_yrs) - 1 if span_yrs > 0 else np.nan
    return dict(final=bal, ret=ret, cagr=cagr, maxdd=maxdd, pf=pf, taken=taken, skipped=skipped)


STRATS = {"all-days": lambda d: d, "MWF (Mon/Wed/Fri)": lambda d: d[d.wd.isin([0, 2, 4])],
          "Monday-only": lambda d: d[d.wd == 0]}

for path, tag in [("simulations/trades_panel_daily.csv", "BASE slippage"),
                  ("simulations/trades_panel_daily_slip2x.csv", "2x slippage")]:
    base = load(path)
    for start in (5_000, 20_000):
        print("\n===== $%s start | %s =====" % (f"{start:,}", tag))
        print("  %-18s %10s %8s %8s %8s %7s %6s/%s" % ("strategy", "final$", "return", "CAGR", "maxDD", "PF", "took", "skip"))
        for name, filt in STRATS.items():
            d = filt(base).sort_values("ent").reset_index(drop=True)
            r = simulate(d, start)
            print("  %-18s %10s %+7.0f%% %+6.0f%% %6.0f%% %7.2f %5d/%d" % (
                name, f"${r['final']:,.0f}", r["ret"]*100, r["cagr"]*100, r["maxdd"]*100, r["pf"], r["taken"], r["skipped"]))
