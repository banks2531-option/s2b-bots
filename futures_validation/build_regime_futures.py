"""Build the daily regime series (SPY 20/50 SMA stack -> UP/DOWN/CHOP) from
yfinance (free), point-in-time: regime used on day D is computed from closes
through D-1. Also detect ES continuous-contract roll days by comparing ES
overnight RTH gaps to ^GSPC daily gaps.
"""
import pandas as pd
import numpy as np
import yfinance as yf

OUTDIR = r"C:\Users\FixUser123\Documents\fable options\futures_validation"

spy = yf.download("SPY", start="2024-10-01", end="2026-03-05", progress=False, auto_adjust=False)
gspc = yf.download("^GSPC", start="2025-02-20", end="2026-03-05", progress=False, auto_adjust=False)
print("SPY rows:", len(spy), "GSPC rows:", len(gspc))

close = spy["Close"]
if isinstance(close, pd.DataFrame):
    close = close.iloc[:, 0]
sma20 = close.rolling(20).mean()
sma50 = close.rolling(50).mean()

regime = pd.Series("CHOP", index=close.index)
regime[(close > sma20) & (sma20 > sma50)] = "UP"
regime[(close < sma20) & (sma20 < sma50)] = "DOWN"

# point-in-time: shift forward one session (regime for session D = state at D-1 close)
pit = regime.shift(1).dropna()
out = pd.DataFrame({"regime": pit})
out.index = out.index.strftime("%Y-%m-%d")
out = out[(out.index >= "2025-02-25") & (out.index <= "2026-03-02")]
out.to_csv(f"{OUTDIR}\\regime_daily.csv")
print(out["regime"].value_counts())
print("\nregime by month:")
tmp = out.copy(); tmp["m"] = [d[:7] for d in tmp.index]
print(tmp.groupby("m")["regime"].agg(lambda s: s.value_counts().idxmax()))

# Roll detection: ES overnight RTH gap minus SPX close-to-open... we lack SPX open
# intraday alignment; use close-to-close differences instead:
# ES RTH close-to-close pct vs ^GSPC close-to-close pct. A roll shows as a one-day
# divergence (basis jump) then normal tracking.
bars = pd.read_parquet(f"{OUTDIR}\\es_1m.parquet")
rth = bars[(bars["et_time"] >= "09:30") & (bars["et_time"] <= "15:59")]
es_close = rth.groupby("et_date").last()["close"]
es_ret = es_close.pct_change() * 100

g = gspc["Close"]
if isinstance(g, pd.DataFrame):
    g = g.iloc[:, 0]
g.index = g.index.strftime("%Y-%m-%d")
g_ret = g.pct_change() * 100

both = pd.DataFrame({"es": es_ret, "spx": g_ret.reindex(es_ret.index)}).dropna()
both["diff"] = both["es"] - both["spx"]
sus = both[both["diff"].abs() > 0.25]
print("\nES-vs-SPX close-to-close divergence >0.25% (roll candidates):")
print(sus.round(3).to_string())
