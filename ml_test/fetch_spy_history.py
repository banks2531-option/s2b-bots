"""Pull extended SPY + ^VIX daily history (free yfinance) for point-in-time
technical features. Start well before the panel (2024-10) so 50-day SMA and
21-day RV are fully warm by the first entry (2025-03-04). Output: spy_daily.csv
"""
import yfinance as yf
import pandas as pd

START, END = "2024-09-01", "2026-03-05"  # end past last entry (2026-02-27)

spy = yf.download("SPY", start=START, end=END, auto_adjust=False, progress=False)
vix = yf.download("^VIX", start=START, end=END, auto_adjust=False, progress=False)

# flatten potential multiindex columns
def flat(df, prefix):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df[["Open","High","Low","Close","Volume"]].copy()
    df.columns = [f"{prefix}_{c.lower()}" for c in df.columns]
    return df

s = flat(spy, "spy")
v = flat(vix, "vix")[["vix_close"]]
out = s.join(v, how="left")
out.index.name = "date"
out = out.reset_index()
out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
out.to_csv("spy_daily.csv", index=False)
print("rows", len(out), "range", out.date.iloc[0], "->", out.date.iloc[-1])
print(out.head(3).to_string())
print("nan vix:", out.vix_close.isna().sum())
