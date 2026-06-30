"""OOS validation of the mechanical 'underpriced earnings straddle' rule:
   BUY the ATM straddle iff implied_move < T x (the name's historical mean earnings move).

Discipline (C4/C5): chronological H1-tune / H2-test split. The threshold T is chosen ONLY on H1
(in-sample); the LOCKED T is then tested on H2 (out-of-sample). Plus random-selection nulls and a
2x transaction-cost stress. The rule must stay positive net-of-cost OOS, beat the null, and survive
2x costs — or it was overfit.
"""
import csv, random, statistics
random.seed(7)

ROWS = list(csv.DictReader(open("research/convexity/events.csv")))
for r in ROWS:
    r["ratio"] = float(r["implied_vs_hist_ratio"]) if r["implied_vs_hist_ratio"] not in ("", "None") else None
    r["pnl"] = float(r["pnl"]); r["cost"] = float(r["straddle_cost"]) * 100; r["date"] = r["earnings_date"]
ROWS = [r for r in ROWS if r["ratio"] is not None]
ROWS.sort(key=lambda r: r["date"])
N = len(ROWS)
print("events: %d  | date range %s -> %s  | names %d"
      % (N, ROWS[0]["date"], ROWS[-1]["date"], len(set(r["ticker"] for r in ROWS))))

# chronological split: first 55% (tune) / last 45% (test)
cut = int(N * 0.55)
H1, H2 = ROWS[:cut], ROWS[cut:]
print("H1 (tune): %d events %s..%s   H2 (test/OOS): %d events %s..%s"
      % (len(H1), H1[0]["date"], H1[-1]["date"], len(H2), H2[0]["date"], H2[-1]["date"]))


def book(rows, sel, cpct):
    """net P&L of buying straddles where sel(row) is True, minus cpct round-trip cost of premium."""
    picks = [r for r in rows if sel(r)]
    net = sum(r["pnl"] - cpct * r["cost"] for r in picks)
    wins = sum(1 for r in picks if r["pnl"] > 0)
    gp = sum(r["pnl"] - cpct * r["cost"] for r in picks if r["pnl"] - cpct * r["cost"] > 0)
    gl = sum(-(r["pnl"] - cpct * r["cost"]) for r in picks if r["pnl"] - cpct * r["cost"] < 0)
    return len(picks), wins, net, (gp / gl if gl else float("inf"))


# 1) TUNE T on H1 only (pre-registered candidate set), maximizing net @ realistic 10% cost
CAND = [0.7, 0.8, 0.9, 1.0, 1.1]
COST = 0.10
h1scores = [(T, book(H1, lambda r, T=T: r["ratio"] < T, COST)[2]) for T in CAND]
Tstar = max(h1scores, key=lambda x: x[1])[0]
print("\n[H1 tune @%.0f%% cost] " % (COST * 100)
      + " ".join("T<%.1f:$%+.0f" % (T, s) for T, s in h1scores) + "  -> LOCKED T=%.1f" % Tstar)

# 2) TEST locked T on H2 (OOS) across cost levels
print("\n[H2 OOS] rule = buy iff implied_vs_hist_ratio < %.1f (locked from H1)" % Tstar)
for c in (0.0, 0.10, 0.20):
    n, w, net, pf = book(H2, lambda r: r["ratio"] < Tstar, c)
    print("  @%2.0f%% cost: n=%d win%%=%.0f net=$%+.0f PF=%.2f" % (c * 100, n, (w / n * 100 if n else 0), net, pf))

# 3) NULL on H2: random selection of the same count, 10000 shuffles, @10% cost
nsel = sum(1 for r in H2 if r["ratio"] < Tstar)
rnet = book(H2, lambda r: r["ratio"] < Tstar, COST)[2]
allnet = [r["pnl"] - COST * r["cost"] for r in H2]
nulls = sorted(sum(random.sample(allnet, nsel)) for _ in range(10000))
p = sum(1 for x in nulls if x >= rnet) / len(nulls)
print("  NULL @10%% cost: random %d-pick mean $%+.0f | rule net $%+.0f | p(random>=rule)=%.3f"
      % (nsel, statistics.mean(nulls), rnet, p))

# 4) ROBUSTNESS: the locked rule, per calendar year (decay check), @10% cost
print("\n[robustness] locked rule per year @10%% cost:")
for yr in sorted(set(r["date"][:4] for r in ROWS)):
    yrows = [r for r in ROWS if r["date"][:4] == yr]
    n, w, net, pf = book(yrows, lambda r: r["ratio"] < Tstar, COST)
    print("  %s: n=%d net=$%+.0f PF=%.2f" % (yr, n, net, pf))

# 5) VERDICT
n, w, net, pf = book(H2, lambda r: r["ratio"] < Tstar, COST)
n2, w2, net2, pf2 = book(H2, lambda r: r["ratio"] < Tstar, 0.20)
ok = net > 0 and p < 0.05 and net2 > 0
print("\n=== VERDICT: %s ===" % ("OOS-VALIDATED (positive net, beats null, survives 2x cost)" if ok
      else "FAILS OOS (overfit / cost-fragile / null)"))
