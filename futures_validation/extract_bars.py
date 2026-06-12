"""Extract ES 1-min bars from the legacy api_cache.db (read-only) into a local
parquet, convert UTC->ET, and run data-quality checks:
  - per-day RTH (09:30-16:00 ET) bar counts
  - missing sessions vs expected weekday calendar
  - roll-gap detection on the continuous series (calendar roll, unadjusted)
"""
import sqlite3, json
import pandas as pd
import numpy as np
from zoneinfo import ZoneInfo

DB = r"E:\BanksBackup\trading-bot\backtests\cache\api_cache.db"
OUT = r"C:\Users\FixUser123\Documents\fable options\futures_validation\es_1m.parquet"
ET = ZoneInfo("America/New_York")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

rows = cur.execute("SELECT key, value FROM cache WHERE key LIKE 'futures:es_1m_%'").fetchall()
frames = []
empty_days = []
for k, v in rows:
    ds = k.rsplit("_", 1)[1]
    bars = json.loads(v)
    if not bars:
        empty_days.append(ds)
        continue
    df = pd.DataFrame(bars)
    df["cache_day"] = ds
    frames.append(df)
con.close()

df = pd.concat(frames, ignore_index=True)
df["dt_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
df["dt_et"] = df["dt_utc"].dt.tz_convert(ET)
df = df.sort_values("ts").drop_duplicates(subset="ts").reset_index(drop=True)
df["et_date"] = df["dt_et"].dt.strftime("%Y-%m-%d")
df["et_time"] = df["dt_et"].dt.strftime("%H:%M")

print(f"total bars: {len(df)}  cache days: {len(rows)}  empty cache days: {len(empty_days)}")
if empty_days:
    print("  empty:", empty_days[:10])
print(f"UTC range: {df['dt_utc'].min()} .. {df['dt_utc'].max()}")

# RTH = 09:30-15:59 ET inclusive bar starts (390 bars)
rth = df[(df["et_time"] >= "09:30") & (df["et_time"] <= "15:59")]
counts = rth.groupby("et_date").size()
print(f"\nRTH sessions: {len(counts)}")
print(f"bar-count distribution: min={counts.min()} p5={counts.quantile(.05):.0f} "
      f"median={counts.median():.0f} max={counts.max()}")
bad = counts[counts < 370]
print(f"sessions with <370 RTH bars: {len(bad)}")
for d, n in bad.items():
    # show what part of the session exists
    times = rth[rth["et_date"] == d]["et_time"]
    print(f"  {d}: {n} bars  ({times.min()} .. {times.max()})")

# Missing weekdays in span
all_days = pd.date_range(counts.index.min(), counts.index.max(), freq="B").strftime("%Y-%m-%d")
missing = sorted(set(all_days) - set(counts.index))
print(f"\nweekdays with no RTH session: {len(missing)}")
print(" ", missing)

# Roll detection: day-over-day gap between RTH close and next RTH open vs NQ?
# Simple: flag |overnight gap| > 1.2% (real overnight moves rarely exceed; calendar
# roll adds carry basis jump). Also check around quarterly expiries.
rth_open = rth.groupby("et_date").first()["open"]
rth_close = rth.groupby("et_date").last()["close"]
gap = (rth_open - rth_close.shift(1)) / rth_close.shift(1) * 100
big = gap[gap.abs() > 1.0]
print("\novernight RTH gaps >1.0%:")
print(big.to_string())

# Also check the 18:00 ET reopen gap (roll usually shows there cleanly)
glob_last = df.groupby("et_date").last()  # last bar of each ET date (~17:00 close Mon-Thu)
# reopen: first bar at/after 18:00 ET each date
evening = df[df["et_time"] >= "18:00"].groupby("et_date").first()
reopen_gap = (evening["open"] - glob_last["close"].reindex(evening.index)) / glob_last["close"].reindex(evening.index) * 100
big2 = reopen_gap[reopen_gap.abs() > 0.35]
print("\n18:00 ET reopen gaps >0.35% (roll candidates):")
print(big2.to_string())

# price sanity
print("\nRTH close samples:")
for d in ["2025-03-03", "2025-04-07", "2025-08-28", "2026-02-27"]:
    if d in rth_close.index:
        print(f"  {d}: {rth_close[d]}")

df[["ts", "open", "high", "low", "close", "volume", "et_date", "et_time"]].to_parquet(OUT, index=False)
print(f"\nwrote {OUT}")
