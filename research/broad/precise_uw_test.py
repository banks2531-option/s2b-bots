"""Precise UW 'community-consensus' signal test (user-specified, 2026-06-15):
Vol/OI>1 AND premium>=P AND alert_rule==RepeatedHitsAscendingFill AND directional,
used as a directional entry. Tune P on H1, test H2 OOS. (GEX cross-reference NOT
included — no GEX data locally; flagged separately.)
Reuses research/intraday/build_outcomes bar logic on the cached top-80 bars.
"""
import os, sys, json
import numpy as np, pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "intraday"))
import build_outcomes as bo

ALERTS = os.path.join(HERE, "..", "alerts_panel.parquet")
CORE = json.load(open(os.path.join(HERE, "..", "intraday", "liquid_core.json")))

def qualifying():
    df = pd.read_parquet(ALERTS)
    df = df[(df.is_index == False) & (df.dir != 0) & (df.ticker.isin(CORE))].copy()
    df = df[(df.vol_oi_ratio > 1.0) & (df.alert_rule == "RepeatedHitsAscendingFill")]
    df["ts_et"] = pd.to_datetime(df["ts_et"])
    hh = df["ts_et"].dt.strftime("%H:%M")
    df = df[(hh >= "09:30") & (hh <= "15:59")].copy()
    df["adate"] = df["ts_et"].dt.strftime("%Y-%m-%d")
    return df[["id", "ticker", "adate", "ts_et", "dir", "total_premium", "half"]]

def build(alerts):
    rows = []
    LAG = np.timedelta64(60, "s"); H60 = np.timedelta64(60, "m")
    for tk, grp in alerts.groupby("ticker"):
        bars = bo.load_bars(tk)
        if bars is None or bars.empty:
            continue
        by = {d: g.sort_values("ts_utc") for d, g in bars.groupby("date")}
        for _, a in grp.iterrows():
            bd = by.get(a["adate"])
            if bd is None or bd.empty:
                continue
            ts = bd["ts_utc"].to_numpy(); o = bd["o"].to_numpy(); c = bd["c"].to_numpy()
            ats = pd.Timestamp(a["ts_et"]).tz_convert("UTC").tz_localize(None).to_datetime64()
            ei = np.searchsorted(ts, ats + LAG, "left")
            if ei >= len(ts) or o[ei] <= 0:
                continue
            d = int(a["dir"]); entry = o[ei]
            xi = np.searchsorted(ts, ts[ei] + H60, "left")
            r60 = d * (o[xi] / entry - 1) if xi < len(ts) else np.nan
            reod = d * (c[-1] / entry - 1)
            rows.append({"half": a["half"], "total_premium": a["total_premium"], "ret_60": r60, "ret_eod": reod})
    return pd.DataFrame(rows)

def grosspf(r):
    r = r[~np.isnan(r)]; pos = r[r > 0].sum(); neg = -r[r < 0].sum()
    return pos / neg if neg > 0 else np.nan

def main():
    a = qualifying()
    print(f"precise-signal qualifying RTH alerts (top-80 core): {len(a)}  H1/H2={a.half.value_counts().to_dict()}", flush=True)
    df = build(a)
    print(f"with outcomes: {len(df)}", flush=True)
    # tune premium threshold on H1 (consensus floor 50k-100k+)
    h1 = df[df.half == "H1"]
    best = None
    for P in [50000, 100000, 250000, 500000, 1000000]:
        s = h1[h1.total_premium >= P]
        if len(s) < 200: continue
        m = s.ret_eod.mean()
        print(f"  H1 P>={P:>9}: n={len(s):>5} EOD mean={100*m:+.3f}% grossPF={grosspf(s.ret_eod.to_numpy()):.2f}", flush=True)
        if best is None or m > best[1]: best = (P, m)
    Pstar = best[0]
    print(f"  -> P*={Pstar}", flush=True)
    h2 = df[(df.half == "H2") & (df.total_premium >= Pstar)]
    for H in ["ret_60", "ret_eod"]:
        r = h2[H].to_numpy()
        print(f"  H2 (OOS) {H}: n={np.sum(~np.isnan(r))} winrate={100*np.nanmean(r>0):.1f}% mean={100*np.nanmean(r):+.3f}% grossPF={grosspf(r):.2f}", flush=True)
    print("\nNOTE: gross-PF is the underlying-move CEILING; option costs only subtract. PF 2.0 needs this >> 2.", flush=True)

if __name__ == "__main__":
    main()
