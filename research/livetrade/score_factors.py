"""Part 3 — dissect every live bot-B spread against the factor reference.

Builds a per-trade factor matrix (broker-truth 81 spreads), scores each applicable
factor +1/0/-1 per docs/options-trade-factor-reference.md, with broker-truth P&L as
the label. Underlying daily history + earnings via yfinance. Entry credit + time
matched from the bot's own log (credit = |OPEN price|).

Outputs: livetrade_factors.csv (raw), livetrade_scored.csv (+1/0/-1), and a
winner-vs-loser comparison to stdout. v1 rubric — documented inline.
"""
import os, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
DD = os.path.join(HERE, "..", "..", "droplet_data")
TRUTH = os.path.join(DD, "tradier_truth_outcomes.csv")
BOTLOG = os.path.join(DD, "options_trades_live_b.csv")

BULLISH = {"bull_put_spread", "bull_call_spread"}
CREDIT = {"bull_put_spread", "bear_call_spread"}

def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))

def load_prices(tickers):
    px = {}
    raw = yf.download(tickers + ["SPY"], start="2025-10-01", end="2026-06-01",
                      progress=False, auto_adjust=False, group_by="ticker")
    for t in tickers + ["SPY"]:
        try:
            d = raw[t].dropna(subset=["Close"]).copy()
        except Exception:
            continue
        if d.empty:
            continue
        d["sma20"] = d.Close.rolling(20).mean(); d["sma50"] = d.Close.rolling(50).mean()
        d["rsi"] = rsi(d.Close)
        d["ret5"] = d.Close.pct_change(5)
        d["rv10"] = d.Close.pct_change().rolling(10).std() * np.sqrt(252)
        tr = pd.concat([d.High - d.Low, (d.High - d.Close.shift()).abs(), (d.Low - d.Close.shift()).abs()], axis=1).max(axis=1)
        d["atr"] = tr.rolling(14).mean()
        d.index = pd.to_datetime(d.index).strftime("%Y-%m-%d")
        px[t] = d
    vix = yf.download("^VIX", start="2025-10-01", end="2026-06-01", progress=False, auto_adjust=False)
    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = vix.columns.get_level_values(0)
    vix.index = pd.to_datetime(vix.index).strftime("%Y-%m-%d")
    close = vix["Close"]
    pct = close.rolling(180, min_periods=40).apply(lambda x: (x.iloc[-1] >= x).mean())
    return px, close, pct

def earnings(tickers):
    em = {}
    for t in tickers:
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=16)
            em[t] = sorted(pd.to_datetime([i.date() for i in ed.index])) if ed is not None else []
        except Exception:
            em[t] = []
    return em

def asof(d, date):
    """row of daily frame at-or-before date (string compare works on YYYY-MM-DD)."""
    idx = d.index[d.index <= date]
    return d.loc[idx[-1]] if len(idx) else None

def main():
    t = pd.read_csv(TRUTH)
    bot = pd.read_csv(BOTLOG)
    bot["date"] = bot.Timestamp.str[:10]; bot["hour"] = bot.Timestamp.str[11:13].astype(int)
    opens = bot[bot.Action == "OPEN"].copy()
    tickers = sorted(t.ticker.unique())
    px, vix, vixpct = load_prices(tickers)
    em = earnings(tickers)

    rows = []
    for _, r in t.iterrows():
        tk, od, strat = r.ticker, r.open_date, r.strategy_type
        width = abs(r.long_strike - r.short_strike)
        is_credit = strat in CREDIT; is_bull = strat in BULLISH
        # match bot-log OPEN for credit + entry time
        m = opens[(opens.Symbol == tk) & (opens.date == od) & (opens.Strategy == strat)]
        credit = abs(m.Price.iloc[0]) if len(m) else np.nan
        hour = int(m.hour.iloc[0]) if len(m) else np.nan
        d = px.get(tk); spx = px.get("SPY")
        row = {"ticker": tk, "open_date": od, "strategy": strat, "pnl": r.pnl_dollars,
               "win": int(r.pnl_dollars > 0), "is_credit": is_credit, "is_bull": is_bull,
               "width": width, "credit": credit,
               "dte": (pd.Timestamp(r.expiry) - pd.Timestamp(od)).days,
               "hold_days": (pd.Timestamp(r.close_date) - pd.Timestamp(od)).days, "hour": hour}
        # structure economics
        row["credit_to_width"] = credit / width if (is_credit and width) else np.nan
        if is_credit and not np.isnan(credit):
            row["max_profit"] = credit * 100; row["max_loss"] = (width - credit) * 100
            row["rr"] = row["max_loss"] / row["max_profit"] if row["max_profit"] else np.nan
        # underlying context
        ur = asof(d, od) if d is not None else None
        if ur is not None:
            spot = ur.Close; row["spot"] = spot
            # OTM cushion: signed distance spot->short strike in the SAFE direction
            if strat == "bull_put_spread": cushion = (spot - r.short_strike) / spot
            elif strat == "bear_call_spread": cushion = (r.short_strike - spot) / spot
            else: cushion = np.nan  # debit spreads: cushion concept differs
            row["otm_cushion"] = cushion
            row["cushion_atr"] = (cushion * spot) / ur.atr if ur.atr and not np.isnan(ur.atr) else np.nan
            row["name_vs_sma20"] = spot / ur.sma20 - 1; row["name_vs_sma50"] = spot / ur.sma50 - 1
            row["rsi"] = ur.rsi; row["ret5"] = ur.ret5; row["rv10"] = ur.rv10
        if spx is not None:
            sr = asof(spx, od)
            if sr is not None:
                row["spy_vs_sma20"] = sr.Close / sr.sma20 - 1; row["spy_uptrend"] = int(sr.Close > sr.sma20)
        row["vix"] = float(vix[vix.index <= od].iloc[-1]) if (vix.index <= od).any() else np.nan
        row["vix_pct"] = float(vixpct[vixpct.index <= od].iloc[-1]) if (vixpct.index <= od).any() else np.nan
        # earnings: held through?
        eds = em.get(tk, [])
        nxt = [e for e in eds if pd.Timestamp(od) <= e <= pd.Timestamp(r.expiry)]
        future = [e for e in eds if e >= pd.Timestamp(od)]
        row["held_through_earnings"] = int(len(nxt) > 0)
        row["days_to_earnings"] = (min(future) - pd.Timestamp(od)).days if future else np.nan
        row["weekday"] = pd.Timestamp(od).day_name()
        rows.append(row)

    f = pd.DataFrame(rows)
    # concurrency: open positions already on at each entry (by open->close overlap)
    t2 = t.copy(); t2["o"] = pd.to_datetime(t2.open_date); t2["c"] = pd.to_datetime(t2.close_date)
    conc = []
    for _, r in t2.iterrows():
        conc.append(int(((t2.o < r.o) & (t2.c >= r.o)).sum()))
    f["concurrent_open"] = conc
    f.to_parquet(os.path.join(HERE, "livetrade_factors.parquet"))
    f.to_csv(os.path.join(HERE, "livetrade_factors.csv"), index=False)

    # ---------- v1 scoring rubric (+1 favorable / 0 neutral / -1 adverse, vs structure) ----------
    s = pd.DataFrame(index=f.index)
    def sc(mask_fav, mask_adv): return np.where(mask_fav, 1, np.where(mask_adv, -1, 0))
    # F2 credit/width: 0.25-0.40 favorable, <0.15 or >0.5 adverse (credit spreads only)
    cw = f.credit_to_width
    s["F2_credit_width"] = np.where(f.is_credit, sc((cw >= 0.25) & (cw <= 0.40), (cw < 0.15) | (cw > 0.5)), 0)
    # E6 OTM cushion in ATR: >=2 fav, <1 adverse (credit spreads)
    s["E6_cushion_atr"] = np.where(f.is_credit, sc(f.cushion_atr >= 2.0, f.cushion_atr < 1.0), 0)
    # E2 DTE: bot ran weeklies; 3-10 fav, 0-1 adverse (gamma), >21 neutral
    s["E2_dte"] = sc((f.dte >= 3) & (f.dte <= 10), f.dte <= 1)
    # D1 earnings: held-through adverse
    s["D1_earnings"] = np.where(f.held_through_earnings == 1, -1, 1)
    # A1/B1 trend alignment: bullish trade wants name above sma20 & spy uptrend
    align = np.where(f.is_bull, (f.name_vs_sma20 > 0).astype(int), (f.name_vs_sma20 < 0).astype(int))
    s["AB_trend_align"] = sc(align == 1, align == 0)
    # B2 RSI not stretched against trade: bullish & rsi<70 ok, >75 adverse; bearish & rsi>30 ok, <25 adverse
    s["B2_rsi"] = np.where(f.is_bull, sc(f.rsi < 70, f.rsi > 78), sc(f.rsi > 30, f.rsi < 22))
    # C/A2 VIX for premium sellers: mid-high fav (pct .3-.85), extreme-low adverse
    s["A2_vix"] = np.where(f.is_credit, sc((f.vix_pct >= 0.3) & (f.vix_pct <= 0.9), f.vix_pct < 0.15), 0)
    # F3 RR: max_loss/max_profit <=3 fav, >5 adverse (credit)
    s["F3_rr"] = np.where(f.is_credit, sc(f.rr <= 3, f.rr > 5), 0)
    # I3 concurrency: <=3 fav, >6 adverse
    s["I3_concurrency"] = sc(f.concurrent_open <= 3, f.concurrent_open > 6)
    # strategy-type prior: directional debit (bull_call/bear_put) flagged risky given bot-B record
    s["F1_structure"] = np.where(f.strategy.isin(["bull_call_spread", "bear_put_spread"]), -1, 0)

    s["factor_score"] = s.sum(axis=1)
    out = pd.concat([f[["ticker", "open_date", "strategy", "pnl", "win"]], s], axis=1)
    out.to_csv(os.path.join(HERE, "livetrade_scored.csv"), index=False)

    # ---------- winner vs loser comparison ----------
    print(f"81 spreads | wins {f.win.sum()} losers {(1-f.win).sum()} | net ${f.pnl.sum():.0f}")
    print("\n=== factor_score vs outcome ===")
    print(out.groupby("win").factor_score.agg(["mean", "median", "min", "max"]).round(2))
    print("\n=== mean factor_score by score bucket -> win rate & avg P&L ===")
    out["bucket"] = pd.cut(out.factor_score, [-99, -1, 1, 3, 99], labels=["<=-1", "0..1", "2..3", ">=4"])
    print(out.groupby("bucket").agg(n=("win", "size"), winrate=("win", "mean"), avg_pnl=("pnl", "mean")).round(2))
    print("\n=== per-factor: mean score among winners vs losers (gap = discriminating power) ===")
    cols = [c for c in s.columns if c != "factor_score"]
    cmp = pd.DataFrame({"winners": s[f.win == 1][cols].mean(), "losers": s[f.win == 0][cols].mean()})
    cmp["gap"] = (cmp.winners - cmp.losers); print(cmp.round(2).sort_values("gap", ascending=False))
    print("\n=== raw factor means: winners vs losers ===")
    for c in ["credit_to_width", "cushion_atr", "dte", "held_through_earnings", "rr", "vix_pct", "rsi", "concurrent_open"]:
        if c in f:
            print(f"  {c:22s} win={f[f.win==1][c].mean():.3f}  lose={f[f.win==0][c].mean():.3f}")
    print("\n=== by strategy ===")
    print(f.groupby("strategy").agg(n=("pnl", "size"), winrate=("win", "mean"), net=("pnl", "sum")).round(2))

if __name__ == "__main__":
    main()
