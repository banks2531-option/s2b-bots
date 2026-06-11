"""Hold-out tests for the two post-hoc candidate configs surfaced by the batch.

Candidates (selected from H1 sweep + full-window ablations — i.e., NOT
independent discoveries; H2 is their first contact with unseen data):
  A. wide stop 1.5x / TP 50% / delta cap 0.35 (best contiguous sweep corner)
  B. spec config minus the IV-rank filter (best ablation)
  C. A + B combined
Each candidate also gets its H1 run where missing, for half-vs-half consistency.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v345 as eng
import sim_proposed as sp

H1 = ('2025-03-03', '2025-08-28')
H2 = ('2025-08-29', '2026-02-27')

RUNS = [
    ('ho_A_s15t50d35_h2',       dict(stop_mult=1.5, tp_pct=0.50, delta_cap=0.35, use_regime_gate=True,  use_iv_filter=True,  start=H2[0], end=H2[1])),
    ('ho_B_noiv_h1',            dict(stop_mult=1.0, tp_pct=0.50, delta_cap=0.30, use_regime_gate=True,  use_iv_filter=False, start=H1[0], end=H1[1])),
    ('ho_B_noiv_h2',            dict(stop_mult=1.0, tp_pct=0.50, delta_cap=0.30, use_regime_gate=True,  use_iv_filter=False, start=H2[0], end=H2[1])),
    ('ho_C_s15t50d35_noiv_h1',  dict(stop_mult=1.5, tp_pct=0.50, delta_cap=0.35, use_regime_gate=True,  use_iv_filter=False, start=H1[0], end=H1[1])),
    ('ho_C_s15t50d35_noiv_h2',  dict(stop_mult=1.5, tp_pct=0.50, delta_cap=0.35, use_regime_gate=True,  use_iv_filter=False, start=H2[0], end=H2[1])),
]


def main():
    eng.TICKER_BLACKLIST.clear()
    print('loading shared data...', flush=True)
    alerts_by_date = sp.load_alerts(sp.FLOW_CSV)
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    iv_daily = sp.build_iv_daily(alerts_by_date)
    slip_model = sp.build_slippage_model(alerts_by_date)

    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)

    results = {}
    for tag, c in RUNS:
        t0 = time.time()
        print(f'=== RUN {tag} === {c}', flush=True)
        cfg = {'start': c['start'], 'end': c['end'], 'stop_mult': c['stop_mult'],
               'tp_pct': c['tp_pct'], 'delta_cap': c['delta_cap'],
               'use_regime_gate': c['use_regime_gate'],
               'use_iv_filter': c['use_iv_filter']}
        sim = sp.ProposedSim(polygon, tradier, alerts_by_date, regime, iv_daily,
                             slip_model, closes, cfg)
        summary = sim.run()
        sim.dump_trades(os.path.join(HERE, f'trades_{tag}.csv'))
        with open(os.path.join(HERE, f'summary_{tag}.json'), 'w') as f:
            json.dump({'cfg': cfg, 'summary': summary}, f, indent=2)
        results[tag] = {'cfg': cfg, 'summary': summary}
        print(f'--- {tag}: n={summary["n_trades"]} wr={summary["win_rate"]:.2f} '
              f'pf={summary["profit_factor"]:.2f} pnl={summary["total_pnl"]:.0f} '
              f'dd={summary["max_drawdown"]:.0f} ({time.time()-t0:.0f}s)', flush=True)
        with open(os.path.join(HERE, 'holdout_results.json'), 'w') as f:
            json.dump(results, f, indent=2)
    print('HOLDOUT DONE', flush=True)


if __name__ == '__main__':
    main()
