"""Stage 1 outcomes — join qualifying flow alerts to 1-min bars, compute signed
intraday underlying returns at 30/60/120-min and EOD horizons.

Leakage control: entry = open of the first RTH 1-min bar starting >= alert_ts+60s
(a realistic reaction lag; never the same instant). Exit at horizon = open of the
first bar at/after entry+h; EOD = last RTH bar close (<=15:59 ET). Horizons whose
target lands after 16:00 are NaN (dropped + logged). signed_ret = dir*(exit/entry-1).

Only alerts firing DURING RTH (09:30-15:59 ET) on their own calendar day are used
(intraday reaction; an after-close alert can't be reacted to intraday). Bars are
matched to the alert's own ts_et date, NOT the panel's `entry_date` (which UW rolls
to the next session for after-hours alerts). All time math is datetime64[ns] to
avoid parquet ms/ns resolution bugs.

Pre-registered in research/2026-06-14-intraday-flow-edge-prereg.md.
"""
import os, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BARS_DIR = os.path.join(HERE, "bars")
ALERTS = os.path.join(HERE, "..", "alerts_panel.parquet")
OUT = os.path.join(HERE, "intraday_outcomes.parquet")
RTH_START, RTH_END = "09:30", "15:59"     # alert (and entry) must fall in this window ET
HORIZONS = [30, 60, 120]                   # minutes

def qualifying_alerts(core):
    df = pd.read_parquet(ALERTS)
    df = df[(df.dir != 0) & (df.ticker.isin(core))].copy()
    df["ts_et"] = pd.to_datetime(df["ts_et"])
    hhmm = df["ts_et"].dt.strftime("%H:%M")
    df = df[(hhmm >= RTH_START) & (hhmm <= RTH_END)].copy()   # RTH alerts only
    df["adate"] = df["ts_et"].dt.strftime("%Y-%m-%d")          # alert's own calendar day
    s1 = df[(df.has_sweep == True) & (df.side == "ask")].copy(); s1["family"] = "S1"
    s2 = df[df.has_floor == True].copy(); s2["family"] = "S2"
    cols = ["id", "ticker", "adate", "ts_et", "dir", "total_premium", "half", "family"]
    return pd.concat([s1[cols], s2[cols]], ignore_index=True)

def load_bars(tk):
    p = os.path.join(BARS_DIR, f"{tk}.parquet")
    if not os.path.exists(p):
        return None
    b = pd.read_parquet(p)
    b["ts_et"] = pd.to_datetime(b["ts_et"])
    hhmm = b["ts_et"].dt.strftime("%H:%M")
    b = b[(hhmm >= RTH_START) & (hhmm <= RTH_END)].copy()      # regular trading hours
    b["date"] = b["ts_et"].dt.strftime("%Y-%m-%d")
    # UTC instant as datetime64[ns] (numpy) -> unit-proof searchsorted
    b["ts_utc"] = b["ts_et"].dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]")
    return b

def main():
    core = json.load(open(os.path.join(HERE, "liquid_core.json")))
    alerts = qualifying_alerts(core)
    print(f"qualifying RTH alerts: {len(alerts)} (S1={ (alerts.family=='S1').sum() }, S2={ (alerts.family=='S2').sum() })", flush=True)

    out_rows = []
    cov = {"no_bars_ticker": 0, "no_bars_day": 0, "entry_past_close": 0, "ok": 0}
    LAG = np.timedelta64(60, "s")
    HTD = {h: np.timedelta64(h, "m") for h in HORIZONS}

    for tk, grp in alerts.groupby("ticker"):
        bars = load_bars(tk)
        if bars is None or bars.empty:
            cov["no_bars_ticker"] += len(grp)
            continue
        by_date = {d: g.sort_values("ts_utc") for d, g in bars.groupby("date")}
        for _, al in grp.iterrows():
            bd = by_date.get(al["adate"])
            if bd is None or bd.empty:
                cov["no_bars_day"] += 1
                continue
            ts = bd["ts_utc"].to_numpy()
            o = bd["o"].to_numpy(); c = bd["c"].to_numpy()
            a_ts = pd.Timestamp(al["ts_et"]).tz_convert("UTC").tz_localize(None).to_datetime64()
            ei = np.searchsorted(ts, a_ts + LAG, side="left")
            if ei >= len(ts):
                cov["entry_past_close"] += 1
                continue
            entry = o[ei]
            if entry <= 0:
                cov["entry_past_close"] += 1
                continue
            entry_ts = ts[ei]
            dirn = int(al["dir"])
            row = {k: al[k] for k in ["id", "ticker", "adate", "ts_et", "dir", "total_premium", "half", "family"]}
            row["entry_price"] = float(entry)
            for h in HORIZONS:
                xi = np.searchsorted(ts, entry_ts + HTD[h], side="left")
                row[f"ret_{h}"] = float(dirn * (o[xi] / entry - 1.0)) if xi < len(ts) else np.nan
            row[f"ret_eod"] = float(dirn * (c[-1] / entry - 1.0))   # last RTH bar close
            out_rows.append(row)
            cov["ok"] += 1

    res = pd.DataFrame(out_rows)
    res.to_parquet(OUT)
    print(f"\nwrote {len(res)} outcome rows -> {OUT}", flush=True)
    print("coverage:", cov, flush=True)
    if len(res):
        print("by family/half:", res.groupby(["family", "half"]).size().to_dict(), flush=True)
        print("non-null counts:", {h: int(res[f"ret_{h}"].notna().sum()) for h in [str(x) for x in HORIZONS] + ["eod"]}, flush=True)

if __name__ == "__main__":
    main()
