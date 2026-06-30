"""Build the long-straddle-into-earnings dataset for the Claude-gated convexity backtest.

For each earnings event of a liquid name: buy the ATM straddle the trading day BEFORE the
announcement, sell the trading day AFTER (the canonical "buy the move" convexity trade). Option
prices come from Polygon (flat-rate subscription, no per-query cost); earnings dates + underlying
from yfinance. Writes factors-only cards (point-in-time, NO outcome) for the judges + a hidden
outcomes file for scoring. Post-cutoff dates -> no model memorization.
"""
import warnings; warnings.filterwarnings("ignore")
import json, os, urllib.request
from datetime import timedelta, datetime
import yfinance as yf

KEY = "REDACTED_POLYGON_KEY"
TICKERS = ["AAPL", "NVDA", "TSLA", "AMZN", "META", "AMD", "NFLX", "GOOGL", "MSFT",
           "MU", "AVGO", "CRM", "COIN", "PLTR", "DIS", "QCOM", "INTC", "ORCL", "ADBE",
           "UBER", "SHOP", "SMCI", "MRVL", "SNOW", "NOW", "PYPL", "BA", "JPM", "GS",
           "WMT", "COST", "LLY", "UNH", "BABA", "MSTR", "DELL", "CRWD", "PANW", "ANET"]
START, END = datetime(2023, 1, 1).date(), datetime(2026, 3, 15).date()   # wider window for OOS split
OUT = "research/convexity"
os.makedirs(OUT + "/cards", exist_ok=True)


def poly(path):
    try:
        return json.load(urllib.request.urlopen(
            "https://api.polygon.io" + path + ("&" if "?" in path else "?") + "apiKey=" + KEY, timeout=20))
    except Exception:
        return {}


def occ(t, exp, cp, strike):
    return "O:%s%s%s%08d" % (t, exp.strftime("%y%m%d"), cp, int(round(strike * 1000)))


def day_close(sym, day):
    r = poly("/v2/aggs/ticker/%s/range/1/day/%s/%s" % (sym, day, day))
    res = r.get("results") or []
    return res[0]["c"] if res else None


def fridays_after(d, n=3):
    fr, x = [], d
    while len(fr) < n:
        x += timedelta(days=1)
        if x.weekday() == 4:
            fr.append(x)
    return fr


def cstrikes(spot):
    cs = set()
    for g in (1.0, 2.5, 5.0):
        b = round(spot / g) * g
        for k in (-2, -1, 0, 1, 2):
            cs.add(round(b + k * g, 1))
    return sorted(cs, key=lambda s: abs(s - spot))


def realized_move(px, days, edate):
    """abs % close-to-close move bracketing a past earnings date (entry day -> exit day)."""
    try:
        en = max(d for d in days if d < edate); ex = min(d for d in days if d > edate)
        return abs(float(px.loc[str(ex)]["Close"]) / float(px.loc[str(en)]["Close"]) - 1.0) * 100
    except Exception:
        return None


def sma(px, day, n):
    s = px.loc[:str(day)]["Close"].tail(n)
    return float(s.mean()) if len(s) else None


cards, outcomes = [], {}
idx = 0
for t in TICKERS:
    try:
        ed = yf.Ticker(t).get_earnings_dates(limit=16)
        edates = sorted(set(ix.date() for ix in ed.index))
    except Exception:
        edates = []
    px = yf.download(t, start="2023-06-01", end="2026-03-20", progress=False, auto_adjust=False)
    px.columns = [a if isinstance(a, str) else a[0] for a in px.columns]
    days = [ix.date() for ix in px.index]
    # realized earnings-move history per ticker (all past events) for the "implied vs realized" factor
    past_moves = [m for e in edates for m in [realized_move(px, days, e)] if m is not None]
    hist_mean = round(sum(past_moves) / len(past_moves), 2) if past_moves else None
    hist_max = round(max(past_moves), 2) if past_moves else None

    for edate in edates:
        if not (START <= edate <= END):
            continue
        try:
            entry = max(d for d in days if d < edate); exit = min(d for d in days if d > edate)
        except ValueError:
            continue
        spot = float(px.loc[str(entry)]["Close"])
        got = None
        for exp in fridays_after(exit - timedelta(days=1)):
            for strike in cstrikes(spot)[:6]:
                cs, ps = occ(t, exp, "C", strike), occ(t, exp, "P", strike)
                ec, ep = day_close(cs, str(entry)), day_close(ps, str(entry))
                if ec is None or ep is None:
                    continue
                xc, xp = day_close(cs, str(exit)), day_close(ps, str(exit))
                if xc is None or xp is None:
                    continue
                got = (exp, strike, ec + ep, xc + xp); break
            if got:
                break
        if not got:
            continue
        exp, strike, cost, val = got
        s20, s50 = sma(px, entry, 20), sma(px, entry, 50)
        card = {
            "id": idx, "ticker": t, "earnings_date": str(edate), "entry": str(entry), "exit": str(exit),
            "spot": round(spot, 2), "atm_strike": strike, "expiry": str(exp), "dte_at_entry": (exp - entry).days,
            "straddle_cost": round(cost, 2),
            "implied_move_pct": round(cost / spot * 100, 2),
            "hist_realized_move_mean_pct": hist_mean, "hist_realized_move_max_pct": hist_max,
            "implied_vs_hist_ratio": round((cost / spot * 100) / hist_mean, 2) if hist_mean else None,
            "spot_vs_sma20_pct": round((spot / s20 - 1) * 100, 2) if s20 else None,
            "spot_vs_sma50_pct": round((spot / s50 - 1) * 100, 2) if s50 else None,
        }
        cards.append(card)
        outcomes[idx] = {"pnl": round((val - cost) * 100, 2), "win": int(val > cost),
                         "exit_straddle": round(val, 2)}
        json.dump(card, open(OUT + "/cards/trade_%03d.json" % idx, "w"), indent=2)
        idx += 1
    print("  %s: %d events so far" % (t, idx))

json.dump(outcomes, open(OUT + "/outcomes.json", "w"))
# master events table for the OOS analysis (pure mechanical fields + outcome)
import csv as _csv
with open(OUT + "/events.csv", "w", newline="") as f:
    w = _csv.writer(f)
    w.writerow(["id", "ticker", "earnings_date", "implied_move_pct", "hist_mean", "hist_max",
                "implied_vs_hist_ratio", "straddle_cost", "pnl", "win"])
    for c in cards:
        o = outcomes[c["id"]]
        w.writerow([c["id"], c["ticker"], c["earnings_date"], c["implied_move_pct"],
                    c["hist_realized_move_mean_pct"], c["hist_realized_move_max_pct"],
                    c["implied_vs_hist_ratio"], c["straddle_cost"], o["pnl"], o["win"]])
pnls = [outcomes[i]["pnl"] for i in outcomes]
wins = sum(outcomes[i]["win"] for i in outcomes)
gp = sum(p for p in pnls if p > 0); gl = sum(-p for p in pnls if p < 0)
n = len(pnls)
print("\n=== DATASET: %d earnings-straddle events ===" % n)
if n:
    print("  MECHANICAL (buy every straddle): wins=%d (%.0f%%)  net=$%+.0f  PF=%.2f"
          % (wins, wins / n * 100, sum(pnls), (gp / gl if gl else float("inf"))))
    print("  avg implied move %.2f%%  | avg P&L/trade $%+.0f" % (
        sum(c["implied_move_pct"] for c in cards) / n, sum(pnls) / n))
