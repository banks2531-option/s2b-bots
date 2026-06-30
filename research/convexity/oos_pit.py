"""Point-in-time (look-ahead-free) re-validation of the underpriced-straddle rule.

The original build computed each name's hist_mean earnings move from its WHOLE earnings history
(including events after the trade) -> look-ahead. Here we recompute hist_mean using ONLY earnings
strictly BEFORE each trade (a trader's real information set), rebuild the ratio, and re-run the
exact chronological H1-tune / H2-test OOS validation. The option P&L is unchanged (already correct).
"""
import warnings; warnings.filterwarnings("ignore")
import csv, random, statistics
from datetime import timedelta
import yfinance as yf
random.seed(7)

EV = {r["ticker"]: [] for r in csv.DictReader(open("research/convexity/events.csv"))}
rows = list(csv.DictReader(open("research/convexity/events.csv")))
by_t = {}
for r in rows:
    by_t.setdefault(r["ticker"], []).append(r)

# recompute realized earnings moves per ticker, then point-in-time hist_mean for each event
out = []
for t, ev in by_t.items():
    try:
        ed = yf.Ticker(t).get_earnings_dates(limit=24)
        edates = sorted(set(ix.date() for ix in ed.index))
    except Exception:
        edates = []
    px = yf.download(t, start="2022-06-01", end="2026-03-20", progress=False, auto_adjust=False)
    px.columns = [a if isinstance(a, str) else a[0] for a in px.columns]
    days = [ix.date() for ix in px.index]

    def rmove(e):
        try:
            en = max(d for d in days if d < e); ex = min(d for d in days if d > e)
            return abs(float(px.loc[str(ex)]["Close"]) / float(px.loc[str(en)]["Close"]) - 1.0) * 100
        except Exception:
            return None
    moves = {e: rmove(e) for e in edates}
    from datetime import datetime
    for r in ev:
        edt = datetime.strptime(r["earnings_date"], "%Y-%m-%d").date()
        prior = [moves[e] for e in edates if e < edt and moves[e] is not None]
        if len(prior) < 3:           # need >=3 prior earnings to form a stable point-in-time mean
            continue
        hist_pit = sum(prior) / len(prior)
        im = float(r["implied_move_pct"])
        out.append({"ticker": t, "date": r["earnings_date"], "ratio": im / hist_pit,
                    "pnl": float(r["pnl"]), "cost": float(r["straddle_cost"]) * 100})

out.sort(key=lambda r: r["date"])
N = len(out)
print("point-in-time events (>=3 prior earnings): %d | %s..%s | names %d"
      % (N, out[0]["date"], out[-1]["date"], len(set(r["ticker"] for r in out))))
cut = int(N * 0.55)
H1, H2 = out[:cut], out[cut:]
print("H1 %d (%s..%s)  H2/OOS %d (%s..%s)"
      % (len(H1), H1[0]["date"], H1[-1]["date"], len(H2), H2[0]["date"], H2[-1]["date"]))


def book(rows, T, c):
    picks = [r for r in rows if r["ratio"] < T]
    net = sum(r["pnl"] - c * r["cost"] for r in picks)
    w = sum(1 for r in picks if r["pnl"] > 0)
    gp = sum(r["pnl"] - c * r["cost"] for r in picks if r["pnl"] - c * r["cost"] > 0)
    gl = sum(-(r["pnl"] - c * r["cost"]) for r in picks if r["pnl"] - c * r["cost"] < 0)
    return len(picks), w, net, (gp / gl if gl else float("inf"))


COST = 0.10
CAND = [0.7, 0.8, 0.9, 1.0, 1.1]
Tstar = max(CAND, key=lambda T: book(H1, T, COST)[2])
print("\n[H1 tune @10%%] " + " ".join("T<%.1f:$%+.0f" % (T, book(H1, T, COST)[2]) for T in CAND)
      + "  -> LOCKED T=%.1f" % Tstar)
print("\n[H2 OOS, point-in-time feature] buy iff ratio < %.1f" % Tstar)
for c in (0.0, 0.10, 0.20):
    n, w, net, pf = book(H2, Tstar, c)
    print("  @%2.0f%% cost: n=%d win%%=%.0f net=$%+.0f PF=%.2f" % (c * 100, n, (w / n * 100 if n else 0), net, pf))
nsel = book(H2, Tstar, COST)[0]; rnet = book(H2, Tstar, COST)[2]
allnet = [r["pnl"] - COST * r["cost"] for r in H2]
nulls = sorted(sum(random.sample(allnet, nsel)) for _ in range(10000)) if nsel else [0]
p = sum(1 for x in nulls if x >= rnet) / len(nulls)
print("  NULL @10%%: random %d-pick mean $%+.0f | rule $%+.0f | p=%.3f" % (nsel, statistics.mean(nulls), rnet, p))
n, w, net, pf = book(H2, Tstar, COST); n2, _, net2, _ = book(H2, Tstar, 0.20)
print("\n=== PIT VERDICT: %s ===" % ("HOLDS OOS (look-ahead-free)" if (net > 0 and p < 0.05 and net2 > 0)
      else "FAILS once look-ahead removed -> the edge was the bias"))
