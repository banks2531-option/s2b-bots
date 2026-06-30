"""Multi-asset trend-following test — the literature's #1 validated crisis edge (2026-06-30).

Question this answers: our equity-only S&P-200d trend (research/trend/trend_test.py) WHIPSAWED in 2022.
The literature (Hurst-Ooi-Pedersen; AlphaSimplex/Kaminski) says 2022's CTA profit was MULTI-ASSET —
SHORT bonds (largest leg) + LONG commodities + LONG USD; equities were a net DETRACTOR. So the decisive
test: does a diversified trend basket WITH A SHORT SIDE capture 2022 where equity-only failed?

Basket: SPY (equity), TLT (bonds — short it when rates rise), DBC (commodities), GLD (gold), UUP (USD).
Signal per asset: classic time-series momentum, sign(price - 200d MA), acted on the NEXT day (no
look-ahead). Each asset is risk-scaled to ~equal vol (trailing 60d realized, shifted) and the whole
book is vol-targeted to ~10%/yr so the comparison is apples-to-apples. Long/SHORT per asset (a trend
follower shorts a downtrending asset — that IS the bond-short that made 2022).

Discipline (C4/C5): full-window + H1/H2 split, per-crisis-year breakdown, random-sign NULL at matched
exposure, MA-length robustness (not curve-fit to 200), and a 2x transaction-cost stress.
Window is 2007-03..present (UUP/dollar inception binds) — covers 2008, 2018, 2020, 2022.
Price-only (ignores ETF dividends/cash yield — a wash across the long/short legs being compared)."""
import warnings; warnings.filterwarnings("ignore")
import yfinance as yf
import numpy as np
import pandas as pd

ASSETS = ["SPY", "TLT", "DBC", "GLD", "UUP"]
TARGET_VOL = 0.10          # annualized portfolio vol target
VOL_LB = 60                # trailing window for per-asset realized vol
COST = 0.0005              # per unit of position change, per asset (5 bps); stressed 2x below
CRISIS_YEARS = [2008, 2011, 2015, 2018, 2020, 2022]

# ---- data: align all assets to a common calendar (intersection of trading days) ----
raw = {}
for t in ASSETS:
    c = yf.download(t, start="2006-01-01", end="2026-07-01", progress=False, auto_adjust=False)["Close"]
    if hasattr(c, "columns"):
        c = c.iloc[:, 0]
    raw[t] = c.dropna()
px = pd.DataFrame(raw).dropna()            # common window where ALL five exist
ret = px.pct_change().fillna(0.0)
years = px.index.year


def perf(daily_ret):
    eq = (1 + daily_ret).cumprod()
    n = len(daily_ret)
    cagr = eq.iloc[-1] ** (252.0 / n) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = daily_ret.mean() / (daily_ret.std() + 1e-12) * np.sqrt(252)
    return {"cagr": cagr, "maxdd": dd, "sharpe": sharpe, "total": eq.iloc[-1]}


def asset_signal(close, ma, longshort):
    """+1/-1 (long/short) or 1/0 (long/flat) from price vs MA, acted on the next day."""
    sig = (close > close.rolling(ma).mean()).astype(float)
    sig = sig * 2 - 1 if longshort else sig
    return sig.shift(1).fillna(0.0)


def trend_book(cols, ma=200, longshort=True, cost=COST):
    """Risk-scaled trend book over `cols`. Each asset scaled to equal trailing vol, signed by its own
    200d trend; the basket is then vol-targeted to TARGET_VOL. Returns (net daily ret, gross exposure)."""
    # per-asset inverse-vol weight (trailing, shifted -> no look-ahead)
    realized = ret[cols].rolling(VOL_LB).std().shift(1)
    inv_vol = (1.0 / (realized * np.sqrt(252) + 1e-9)).clip(upper=10.0)   # cap leverage per asset
    legs = {}
    for c in cols:
        sig = asset_signal(px[c], ma, longshort)
        legs[c] = sig * inv_vol[c]                      # signed, risk-scaled position
    pos = pd.DataFrame(legs).fillna(0.0)
    gross = pos.abs().sum(axis=1)
    raw_ret = (pos * ret[cols]).sum(axis=1)
    # portfolio-level vol target on trailing realized portfolio vol (shifted)
    pv = raw_ret.rolling(VOL_LB).std().shift(1) * np.sqrt(252)
    scale = (TARGET_VOL / (pv + 1e-9)).clip(upper=3.0).fillna(0.0)
    scaled_pos = pos.mul(scale, axis=0)
    net = (scaled_pos * ret[cols]).sum(axis=1)
    switches = scaled_pos.diff().abs().sum(axis=1).fillna(0.0)
    net = net - switches * cost
    return net, (scaled_pos.abs().sum(axis=1))


print("Multi-asset trend test | %s..%s | %d common days | %s\n"
      % (px.index[0].date(), px.index[-1].date(), len(px), ", ".join(ASSETS)))

# ---- headline: buy-hold SPY vs equity-only trend vs multi-asset trend ----
bh = perf(ret["SPY"])
eq_only, _ = trend_book(["SPY"], 200, longshort=True)
eq_flat = asset_signal(px["SPY"], 200, False); eq_flat_ret = (eq_flat * ret["SPY"]);
multi, gross = trend_book(ASSETS, 200, longshort=True)
rows = [("BUY & HOLD SPY", bh),
        ("EQUITY-ONLY trend L/S", perf(eq_only)),
        ("MULTI-ASSET trend L/S", perf(multi))]
for name, p in rows:
    print("  %-24s CAGR %5.1f%% | maxDD %6.1f%% | Sharpe %5.2f" % (name, p["cagr"]*100, p["maxdd"]*100, p["sharpe"]))

# ---- THE decisive comparison: per-crisis-year, equity-only vs multi-asset ----
print("\n=== per-year: where does breadth+shorts beat equity-only trend? (the 2022 question) ===")
print("  %-6s %10s %14s %14s" % ("year", "buy-hold", "equity-trend", "multi-trend"))
for yr in CRISIS_YEARS + [2009, 2013, 2017, 2021, 2023, 2024]:
    m = years == yr
    if m.sum() == 0:
        continue
    bhy = (1 + ret["SPY"][m]).prod() - 1
    eqy = (1 + eq_only[m]).prod() - 1
    muy = (1 + multi[m]).prod() - 1
    star = "  <== 2022 delta" if yr == 2022 else ""
    print("  %-6d %+9.1f%% %+13.1f%% %+13.1f%%%s" % (yr, bhy*100, eqy*100, muy*100, star))

# ---- which leg carried 2022? (decompose the multi-asset book by asset) ----
print("\n=== 2022 attribution: per-asset trend P&L contribution ===")
m22 = years == 2022
realized = ret[ASSETS].rolling(VOL_LB).std().shift(1)
inv_vol = (1.0 / (realized * np.sqrt(252) + 1e-9)).clip(upper=10.0)
for c in ASSETS:
    sig = asset_signal(px[c], 200, True)
    leg = (sig * inv_vol[c] * ret[c])[m22]
    pnl = leg.sum()
    avg_sig = sig[m22].mean()
    print("  %-4s 2022 trend-leg P&L %+7.3f (risk units) | avg signal %+.2f (%s)"
          % (c, pnl, avg_sig, "mostly SHORT" if avg_sig < -0.2 else "mostly LONG" if avg_sig > 0.2 else "chop"))

# ---- OOS split: H1 (tune-era) vs H2 (held-out). Signal is param-free, so this checks consistency ----
print("\n=== OOS consistency: H1 vs H2 (param-free signal, so a stability check) ===")
mid = px.index[len(px)//2]
for lbl, mask in [("H1 " + str(px.index[0].year) + "-" + str(mid.year), px.index <= mid),
                  ("H2 " + str(mid.year) + "-" + str(px.index[-1].year), px.index > mid)]:
    pe = perf(eq_only[mask]); pm = perf(multi[mask])
    print("  %-16s equity-trend Sharpe %5.2f | multi-trend Sharpe %5.2f" % (lbl, pe["sharpe"], pm["sharpe"]))

# ---- NULL: does the TREND sign beat random signs at the same gross exposure? ----
print("\n=== NULL: multi-asset trend signs vs random signs (matched exposure, 2000 draws) ===")
np.random.seed(11)
strat_sharpe = perf(multi)["sharpe"]
realized = ret[ASSETS].rolling(VOL_LB).std().shift(1)
inv_vol = (1.0 / (realized * np.sqrt(252) + 1e-9)).clip(upper=10.0)
nulls = []
for _ in range(2000):
    legs = {}
    for c in ASSETS:
        rs = pd.Series(np.where(np.random.rand(len(px)) < 0.5, 1.0, -1.0), index=px.index).shift(1).fillna(0)
        legs[c] = rs * inv_vol[c]
    pos = pd.DataFrame(legs).fillna(0.0)
    rr = (pos * ret[ASSETS]).sum(axis=1)
    pv = rr.rolling(VOL_LB).std().shift(1) * np.sqrt(252)
    scale = (TARGET_VOL / (pv + 1e-9)).clip(upper=3.0).fillna(0.0)
    nulls.append(perf((pos.mul(scale, axis=0) * ret[ASSETS]).sum(axis=1))["sharpe"])
nulls = np.array(nulls)
print("  strategy Sharpe %.2f | random-sign mean %.2f | p(random>=strategy)=%.3f"
      % (strat_sharpe, nulls.mean(), (nulls >= strat_sharpe).mean()))

# ---- robustness across MA length (not curve-fit to 200) ----
print("\n=== robustness across MA length (multi-asset, Sharpe) ===")
print("  " + "  ".join("MA%d:%.2f" % (m, perf(trend_book(ASSETS, m)[0])["sharpe"]) for m in (100, 150, 200, 250)))

# ---- 2x transaction-cost stress ----
print("\n=== 2x cost stress (10 bps/switch/asset) ===")
m2, _ = trend_book(ASSETS, 200, cost=COST*2)
p1, p2 = perf(multi), perf(m2)
print("  multi-trend Sharpe  base %.2f -> 2x-cost %.2f | CAGR %.1f%% -> %.1f%%"
      % (p1["sharpe"], p2["sharpe"], p1["cagr"]*100, p2["cagr"]*100))

# ---- THE decision-relevant test: trend as a crisis-hedge OVERLAY, not a standalone alpha ----
# A long-biased premium-selling book (S2b) GIVES BACK in 2008/2020/2022. Buy-hold SPY is a fair stand-in
# for that crisis-giveback profile. Question: does sprinkling a small, vol-targeted trend sleeve on top
# IMPROVE the combined book's drawdown-adjusted return (Hurst-Ooi-Pedersen "crisis alpha diversifier")?
print("\n=== OVERLAY: buy-hold SPY + w*(multi-asset trend sleeve) — does it improve the COMBINED book? ===")
base = ret["SPY"]
corr_all = base.corr(multi)
m08, m20, m22 = years == 2008, years == 2020, years == 2022
print("  corr(SPY, trend) full %.2f | 2008 %.2f | 2020 %.2f | 2022 %.2f"
      % (corr_all, base[m08].corr(multi[m08]), base[m20].corr(multi[m20]), base[m22].corr(multi[m22])))
print("  %-10s %8s %9s %8s %12s" % ("w_trend", "CAGR", "maxDD", "Sharpe", "2022 ret"))
for w in (0.0, 0.10, 0.20, 0.30, 0.50):
    comb = (1 - w) * base + w * multi
    p = perf(comb)
    r22 = (1 + comb[m22]).prod() - 1
    print("  %-10.0f%% %7.1f%% %8.1f%% %7.2f %11.1f%%" % (w*100, p["cagr"]*100, p["maxdd"]*100, p["sharpe"], r22*100))
