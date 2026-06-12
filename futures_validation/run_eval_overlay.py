"""Topstep-style $50k eval overlay, bot-side simulation.

Rules (per task spec): start $50,000; trailing max drawdown $2,000 computed on
INTRADAY equity peaks (floor stops trailing once it reaches $50,000, i.e.
after +$2,000 of peak profit, matching Topstep's lock-at-start behavior);
daily loss limit $1,000 (breach = bust); profit target +$3,000 = pass.

Every trading day in the data is one eval-attempt start (rolling starts).
Outcomes: PASS (hit +3k), BUST (trailing floor or DLL), CENSORED (data ends).
Equity sampled at every 1-min bar close including open-position MTM, from the
baseline-cost backtest paths, scaled linearly by contract count (1 / 3 / 5 MES;
linear scaling assumption is fair for MES at this size).
"""
import numpy as np

OUTDIR = r"C:\Users\FixUser123\Documents\fable options\futures_validation"
eq = np.load(f"{OUTDIR}\\equity_paths.npy", allow_pickle=True).item()

START, TRAIL, DLL, TARGET = 50_000.0, 2_000.0, 1_000.0, 3_000.0

def run_eval(paths, days, start_idx, contracts):
    bal = START
    peak = START
    floor = START - TRAIL
    cum = 0.0
    for di in range(start_idx, len(days)):
        d = days[di]
        day_start = START + cum
        for (m, v) in paths[d]:
            e = START + cum + v * contracts
            peak = max(peak, e)
            floor = min(max(floor, peak - TRAIL), START)
            if e - day_start <= -DLL:
                return ("BUST_DLL", di - start_idx + 1)
            if e <= floor:
                return ("BUST_TRAIL", di - start_idx + 1)
            if e - START >= TARGET:
                return ("PASS", di - start_idx + 1)
        cum += paths[d][-1][1] * contracts if paths[d] else 0.0
    return ("CENSORED", len(days) - start_idx)

results = {}
for strat in ["f1_midpoint", "f2_orb_long_up", "f3_trend_t12"]:
    paths = eq[strat]
    days = sorted(paths.keys())
    for contracts in (1, 3, 5):
        outcomes = [run_eval(paths, days, s, contracts) for s in range(len(days))]
        n = len(outcomes)
        npass = sum(1 for o, _ in outcomes if o == "PASS")
        nbust = sum(1 for o, _ in outcomes if o.startswith("BUST"))
        ncens = n - npass - nbust
        pass_days = sorted(t for o, t in outcomes if o == "PASS")
        bust_days = sorted(t for o, t in outcomes if o.startswith("BUST"))
        med_pass = pass_days[len(pass_days)//2] if pass_days else None
        med_bust = bust_days[len(bust_days)//2] if bust_days else None
        key = f"{strat}@{contracts}MES"
        results[key] = dict(n=n, npass=npass, nbust=nbust, ncens=ncens,
                            med_days_to_pass=med_pass, med_days_to_bust=med_bust)
        print(f"{key:28s} attempts={n:3d}  PASS={npass:3d} ({100*npass/n:4.1f}%)  "
              f"BUST={nbust:3d} ({100*nbust/n:4.1f}%)  censored={ncens:3d}  "
              f"med_days_to_pass={med_pass}  med_days_to_bust={med_bust}")

import json
with open(f"{OUTDIR}\\eval_overlay_results.json", "w") as f:
    json.dump(results, f, indent=1)
print("\nwrote eval_overlay_results.json")
