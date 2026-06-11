#!/usr/bin/env python3
"""Build point-in-time regime series for the proposed-rules simulation.

Regime (per session d, usable for decisions DURING session d):
  Uses closes up to and including session d-1 ONLY (no lookahead).
  - SPY vs 20d/50d SMA: UP if close > both, DOWN if close < both, else CHOP
  - Cyclical (XLY,XLK,XLI,XLF) vs defensive (XLP,XLU,XLV) 20d total return:
    CYCLICAL_LED if mean cyclical 20d return > mean defensive, else DEFENSIVE_LED

Output: regime_series.csv with columns date,regime,leadership
"""
import pandas as pd
import yfinance as yf

TICKERS = ['SPY', 'XLY', 'XLK', 'XLI', 'XLF', 'XLP', 'XLU', 'XLV']
CYC = ['XLY', 'XLK', 'XLI', 'XLF']
DEF = ['XLP', 'XLU', 'XLV']

px = yf.download(TICKERS, start='2024-10-01', end='2026-03-15',
                 auto_adjust=True, progress=False)['Close']
px = px.dropna(how='all').ffill()

spy = px['SPY']
sma20 = spy.rolling(20).mean()
sma50 = spy.rolling(50).mean()

ret20 = px.pct_change(20)
cyc_ret = ret20[CYC].mean(axis=1)
def_ret = ret20[DEF].mean(axis=1)

rows = []
dates = px.index
for i in range(1, len(dates)):
    d = dates[i]          # session d
    j = i - 1             # info available: through close of session d-1
    if pd.isna(sma50.iloc[j]) or pd.isna(cyc_ret.iloc[j]):
        continue
    c = spy.iloc[j]
    if c > sma20.iloc[j] and c > sma50.iloc[j]:
        regime = 'UP'
    elif c < sma20.iloc[j] and c < sma50.iloc[j]:
        regime = 'DOWN'
    else:
        regime = 'CHOP'
    lead = 'CYCLICAL_LED' if cyc_ret.iloc[j] > def_ret.iloc[j] else 'DEFENSIVE_LED'
    rows.append({'date': d.strftime('%Y-%m-%d'), 'regime': regime, 'leadership': lead})

out = pd.DataFrame(rows)
out.to_csv('regime_series.csv', index=False)
print(out['regime'].value_counts().to_dict())
print(out['leadership'].value_counts().to_dict())
print(out.head(3).to_string(), '\n', out.tail(3).to_string())
