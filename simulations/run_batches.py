#!/usr/bin/env python3
"""Batch driver: loads shared data once, runs all proposed-rules configs.

Order:
  1. proposed_full        — spec config, full window (run b)
  2. h1 sweep (27 cfgs)   — stop x tp x delta-cap plateau on half 1 ONLY (run c)
  3. oos_half2            — locked config on untouched half 2 (run c)
  4. ablations (3)        — full window minus one rule each (run d)

Outputs per tag: trades_<tag>.csv + summary_<tag>.json + batch_results.json
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v345 as eng
import sim_proposed as sp

FULL = ('2025-03-03', '2026-02-27')
H1 = ('2025-03-03', '2025-08-28')
H2 = ('2025-08-29', '2026-02-27')

BASE = {'stop_mult': 1.0, 'tp_pct': 0.50, 'delta_cap': 0.30,
        'use_regime_gate': True, 'use_iv_filter': True}


def cfgs():
    runs = []
    runs.append(('proposed_full', dict(BASE, start=FULL[0], end=FULL[1])))
    # plateau sweep on half 1 only — group by delta cap for cache warmth
    for cap in (0.30, 0.25, 0.35):
        for stop in (0.75, 1.0, 1.5):
            for tp in (0.40, 0.50, 0.60):
                tag = f'h1_s{stop}_t{int(tp*100)}_d{int(cap*100)}'
                runs.append((tag, dict(BASE, start=H1[0], end=H1[1],
                                       stop_mult=stop, tp_pct=tp, delta_cap=cap)))
    runs.append(('oos_half2', dict(BASE, start=H2[0], end=H2[1])))
    runs.append(('abl_no_regime', dict(BASE, start=FULL[0], end=FULL[1],
                                       use_regime_gate=False)))
    runs.append(('abl_no_iv', dict(BASE, start=FULL[0], end=FULL[1],
                                   use_iv_filter=False)))
    runs.append(('abl_no_stop', dict(BASE, start=FULL[0], end=FULL[1],
                                     stop_mult=None)))
    return runs


def main():
    eng.TICKER_BLACKLIST.clear()
    print('loading shared data...', flush=True)
    alerts_by_date = sp.load_alerts(sp.FLOW_CSV)
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    iv_daily = sp.build_iv_daily(alerts_by_date)
    slip_model = sp.build_slippage_model(alerts_by_date)
    print('slip model:', {k: round(v, 3) for k, v in sorted(slip_model.items())}, flush=True)

    if os.environ.get('CACHE_ONLY') == '1':
        polygon = sp.CacheOnlyPolygon(eng.POLYGON_API_KEY)
        print('!! CACHE_ONLY mode', flush=True)
    else:
        polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
        polygon.min_interval = 0.30   # pace below the observed quota to avoid 12s 429 penalties
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)

    results = {}
    for tag, c in cfgs():
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
              f'dd={summary["max_drawdown"]:.0f} ({time.time()-t0:.0f}s, '
              f'api={polygon.api_calls})', flush=True)
        with open(os.path.join(HERE, 'batch_results.json'), 'w') as f:
            json.dump(results, f, indent=2)
    print('ALL BATCHES DONE', flush=True)


if __name__ == '__main__':
    main()
