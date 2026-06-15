"""Broad test, exit leg — sweep TP/stop exits on Monday-S2b over the cached year,
H1-tune / H2-test, baseline-relative, tail-guarded. Cache-only (allow_fresh=0).

Executes research/2026-06-14-managed-vrp-prereg.md exit grid. Reuses scale_s2b's
EquitySim + extended_metrics (which already split H1/H2 by H1_END).
"""
import os, sys, json, itertools
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import scale_s2b as ss, sim_mech as sm, sim_proposed as sp, engine_v345 as eng
from datetime import time as dtime

GRID_TP = [0.35, 0.50, 0.65]
GRID_STOP = [1.0, 1.5, 2.0, 2.5]
BASE = (0.50, 2.0)   # current S2b

def make_strat(tp, stop, tex=1):
    class V(ss.SpyPutspreadDays):
        weekdays = (0,)
        def on_bar(self, dt, bars_so_far, ctx):
            if self.fired or not self.is_entry or dt.time() < dtime(10, 0):
                return []
            self.fired = True
            return [sm.SpreadOrder('SPY', 'bull_put', short_delta=0.40, width=10.0,
                                   dte_target=4, tp_pct=tp, stop_mult=stop,
                                   time_exit_dte=tex, tag='exit_sweep')]
    return V

def run(tp, stop, data, slip_scale=1.0):
    polygon, tradier, iv_daily, slip, closes, regime = data
    sm.MAX_FRESH_API_CALLS = 0          # cache-only
    sm.MAX_CONCURRENT = 3
    sim = ss.EquitySim(polygon, tradier, iv_daily, slip, closes, regime,
                       ss.WINDOW_START, ss.WINDOW_END,
                       risk_pct_equity=0.10, slip_scale=slip_scale)
    sim.run([make_strat(tp, stop)()])
    e = ss.extended_metrics(sim)
    return e

def main():
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY); polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    data = (polygon, tradier, iv_daily, slip, closes, regime)

    rows = []
    for tp, stop in itertools.product(GRID_TP, GRID_STOP):
        e = run(tp, stop, data)
        h1, h2, full = e['h1'], e['h2'], e['full']
        rows.append({'tp': tp, 'stop': stop,
                     'h1_pf': h1.get('pf'), 'h1_n': h1.get('n'), 'h1_pnl': h1.get('pnl'),
                     'h2_pf': h2.get('pf'), 'h2_n': h2.get('n'), 'h2_pnl': h2.get('pnl'),
                     'full_pf': full.get('pf'), 'full_pnl': e['total_pnl'],
                     'maxdd': e['max_drawdown_dollars'],
                     'worst_wk': e['worst_week_losses_only']['sum_losses'],
                     'streak': e['longest_losing_streak']})
        print(f"tp={tp} stop={stop}: H1 PF={h1.get('pf')} (n{h1.get('n')})  "
              f"H2 PF={h2.get('pf')} (n{h2.get('n')}) pnl={h2.get('pnl')}  "
              f"full pnl={e['total_pnl']} maxDD={e['max_drawdown_dollars']} "
              f"worstWk={e['worst_week_losses_only']['sum_losses']} streak={e['longest_losing_streak']}", flush=True)

    json.dump(rows, open(os.path.join(HERE, 'exit_sweep_results.json'), 'w'), indent=2)
    # pick best on H1 PF, then show its H2 vs baseline H2
    base = next(r for r in rows if (r['tp'], r['stop']) == BASE)
    elig = [r for r in rows if (r['h1_n'] or 0) >= 10]
    best = max(elig, key=lambda r: (r['h1_pf'] or 0))
    print("\n=== H1-tuned pick ===")
    print(f"  best-on-H1: tp={best['tp']} stop={best['stop']}  H1 PF={best['h1_pf']} -> H2 PF={best['h2_pf']} (n{best['h2_n']}) pnl={best['h2_pnl']}")
    print(f"  BASELINE  : tp={base['tp']} stop={base['stop']}  H1 PF={base['h1_pf']} -> H2 PF={base['h2_pf']} (n{base['h2_n']}) pnl={base['h2_pnl']}")
    print(f"  full-window pnl: best={best['full_pnl']} vs baseline={base['full_pnl']}; maxDD best={best['maxdd']} vs base={base['maxdd']}")
    print("\n-> simulations/exit_sweep_results.json")

if __name__ == "__main__":
    main()
