"""Systematic trend-following test on the S&P 500 (the real, documented edge that profits in
sustained bears like 2022). Signal: price vs its 200-day moving average (classic time-series
momentum, NO prediction, NO curve-fitting). Two variants:
  - long/flat : long when above the 200d MA, in cash when below (avoid the crashes)
  - long/short: long when above, SHORT when below (profit FROM the decline)
Tested vs buy-and-hold over ~75 years / many bear markets, with: per-year breakdown, max drawdown,
a random-timing NULL, robustness across MA lengths, and a switching-cost stress.
Price-only (ignores dividends on the long side AND cash yield when flat — roughly cancels)."""
import warnings; warnings.filterwarnings("ignore")
import yfinance as yf
import numpy as np
import pandas as pd

px = yf.download("^GSPC", start="1950-01-01", end="2026-03-20", progress=False, auto_adjust=False)
close = px["Close"]
if hasattr(close, "columns"):
    close = close.iloc[:, 0]
close = close.dropna()
ret = close.pct_change().fillna(0.0)
years = close.index.year


def perf(daily_ret, cost_per_switch=0.0, pos=None):
    eq = (1 + daily_ret).cumprod()
    n = len(daily_ret)
    cagr = eq.iloc[-1] ** (252.0 / n) - 1
    peak = eq.cummax(); dd = (eq / peak - 1).min()
    sharpe = daily_ret.mean() / (daily_ret.std() + 1e-12) * np.sqrt(252)
    return {"cagr": cagr, "maxdd": dd, "sharpe": sharpe, "total": eq.iloc[-1]}


def strat(mode, ma=200, cost=0.0):
    sma = close.rolling(ma).mean()
    raw = (close > sma).astype(float)                    # 1 above MA
    if mode == "ls":
        signal = raw * 2 - 1                              # +1/-1
    else:
        signal = raw                                     # 1/0
    pos = signal.shift(1).fillna(0.0)                     # act on YESTERDAY's signal (no look-ahead)
    sr = pos * ret
    switches = pos.diff().abs().fillna(0.0)
    sr = sr - switches * cost                             # cost per position change
    return sr, pos


print("S&P 500 trend-following (200d MA), %s..%s, %d days\n" % (close.index[0].date(), close.index[-1].date(), len(close)))
bh, _ = perf(ret), None
print("%-14s CAGR %5.1f%% | maxDD %6.1f%% | Sharpe %.2f" % ("BUY & HOLD", perf(ret)["cagr"]*100, perf(ret)["maxdd"]*100, perf(ret)["sharpe"]))
for mode, label in [("lf", "LONG/FLAT"), ("ls", "LONG/SHORT")]:
    sr, pos = strat(mode, 200, cost=0.0005)
    p = perf(sr)
    print("%-14s CAGR %5.1f%% | maxDD %6.1f%% | Sharpe %.2f | %% time long %.0f%%"
          % (label, p["cagr"]*100, p["maxdd"]*100, p["sharpe"], (pos > 0).mean()*100))

# per-bear-year breakdown (the question: does it make money when buy-hold crashes?)
print("\n=== returns in the bad years (buy-hold vs trend) ===")
sr_lf, _ = strat("lf", 200, 0.0005); sr_ls, _ = strat("ls", 200, 0.0005)
for yr in [2000, 2001, 2002, 2008, 2018, 2020, 2022]:
    m = years == yr
    bhy = (1 + ret[m]).prod() - 1; lfy = (1 + sr_lf[m]).prod() - 1; lsy = (1 + sr_ls[m]).prod() - 1
    print("  %d: buy-hold %+6.1f%% | long/flat %+6.1f%% | long/short %+6.1f%%" % (yr, bhy*100, lfy*100, lsy*100))

# NULL: does the signal's TIMING beat random days in-market at the same long-fraction?
print("\n=== NULL: trend timing vs random timing (long/flat, same time-in-market) ===")
np.random.seed(7)
sr_lf, pos_lf = strat("lf", 200, 0.0005)
frac = (pos_lf > 0).mean()
strat_sharpe = perf(sr_lf)["sharpe"]
nulls = []
for _ in range(2000):
    rp = pd.Series(np.where(np.random.rand(len(ret)) < frac, 1.0, 0.0), index=ret.index).shift(1).fillna(0)
    nulls.append(perf(rp * ret)["sharpe"])
nulls = np.array(nulls)
print("  strategy Sharpe %.2f | random-timing mean %.2f | p(random>=strategy)=%.3f"
      % (strat_sharpe, nulls.mean(), (nulls >= strat_sharpe).mean()))

# robustness: not curve-fit to MA=200
print("\n=== robustness across MA length (long/flat, Sharpe) ===")
print("  " + "  ".join("MA%d:%.2f" % (m, perf(strat("lf", m, 0.0005)[0])["sharpe"]) for m in (100, 150, 200, 250)))
