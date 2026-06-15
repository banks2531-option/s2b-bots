"""Path 1 — weekly SPY iron condor (non-directional VRP harvest), Monday entry,
~weekly expiry, 20-delta shorts, $10 wings, TP 50%, 1-DTE time exit. Defined-risk.

Tests whether non-directional premium selling on the index reaches a robust PF.
Needs fresh call-side option-bar pulls (cache holds only puts). H1/H2 via
scale_s2b.extended_metrics. Reuses sim_mech iron_condor pricing.
"""
import os, sys, json
from datetime import datetime, time as dtime
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import scale_s2b as ss, sim_mech as sm, sim_proposed as sp, engine_v345 as eng

class SpyWeeklyCondor(sm.MechStrategy):
    name = 'spy_weekly_condor'; symbols = ['SPY']
    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_entry = datetime.strptime(date, '%Y-%m-%d').weekday() == 0   # Monday
    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_entry or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        return [sm.SpreadOrder('SPY', 'iron_condor', short_delta=0.20, width=10.0,
                               dte_target=4, tp_pct=0.50, stop_mult=None,
                               time_exit_dte=1, tag='condor')]

def main():
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY); polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    sm.MAX_FRESH_API_CALLS = 2500          # allow call-side pulls
    sm.MAX_CONCURRENT = 3
    sim = ss.EquitySim(polygon, tradier, iv_daily, slip, closes, regime,
                       ss.WINDOW_START, ss.WINDOW_END, risk_pct_equity=0.10, slip_scale=1.0)
    print("running weekly SPY condor (fresh call-side pulls, ~paced)...", flush=True)
    sim.run([SpyWeeklyCondor()])
    e = ss.extended_metrics(sim)
    out = {'full': e['full'], 'h1': e['h1'], 'h2': e['h2'], 'total_pnl': e['total_pnl'],
           'pnl_per_month': e['pnl_per_month'], 'max_drawdown_dollars': e['max_drawdown_dollars'],
           'worst_week_losses_only': e['worst_week_losses_only'],
           'longest_losing_streak': e['longest_losing_streak']}
    json.dump(out, open(os.path.join(HERE, 'condor_test_results.json'), 'w'), indent=2)
    f = e['full']
    print(f"\n=== WEEKLY SPY CONDOR ===", flush=True)
    print(f"  full: n={f.get('n')} WR={f.get('wr')} PF={f.get('pf')} pnl=${e['total_pnl']} (${e['pnl_per_month']}/mo)", flush=True)
    print(f"  H1 PF={e['h1'].get('pf')} (n{e['h1'].get('n')}) | H2 PF={e['h2'].get('pf')} (n{e['h2'].get('n')})", flush=True)
    print(f"  maxDD=${e['max_drawdown_dollars']} worstWk=${e['worst_week_losses_only']['sum_losses']} streak={e['longest_losing_streak']}", flush=True)
    print("-> simulations/condor_test_results.json", flush=True)

if __name__ == "__main__":
    main()
