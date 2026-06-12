#!/usr/bin/env python3
"""Flow-overlay study: run the DAILY S2b entry panel (Mon-Fri 10:00 ET,
2025-03-03 -> 2026-02-27) after panel_data.py --fetch has filled the cache.

Panel semantics (inference, not portfolio realism — per pre-registration):
  * fixed risk sizing: 5% of $20k = $1,000 vs structure max loss (min 1;
    for this structure qty is 1 contract throughout, identical to the
    validated baseline's fills)
  * MAX_CONCURRENT = 20 and daily-loss halt disabled so every calendar
    entry exists independently of portfolio state
  * baseline costs + slip2x twin (cache-hot)
Writes trades_panel_daily[_slip2x].csv + summary_panel_daily[_slip2x].json.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

import engine_v345 as eng
import sim_proposed as sp
import sim_mech as sm
import strategies_shortlist as sl
import scale_s2b as sc          # NOTE: sets sm.ACCOUNT_START = 20_000
from panel_data import SpyDaily

sm.MAX_CONCURRENT = 20
sm.DAILY_LOSS_HALT = 1e9        # panel = independent-entry inference
sm.MAX_FRESH_API_CALLS = 200    # small safety margin for residual misses


def main():
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)

    for tag, ss in [('panel_daily', 1.0), ('panel_daily_slip2x', 2.0)]:
        out = os.path.join(HERE, f'summary_{tag}.json')
        if os.path.exists(out):
            print(f'skip {tag}: exists', flush=True)
            continue
        print(f"\n=== {tag} slip_scale={ss} ===", flush=True)
        sim = sl.ShortlistSim(polygon, tradier, iv_daily, slip, closes, regime,
                              sl.WINDOW_START, sl.WINDOW_END, slip_scale=ss)
        summary = sim.run([SpyDaily()])
        summary['slip_scale'] = ss
        summary['max_concurrent'] = sm.MAX_CONCURRENT
        summary['risk_dollars'] = sim.risk_dollars
        sim.dump_trades(os.path.join(HERE, f'trades_{tag}.csv'))
        with open(out, 'w') as f:
            json.dump(summary, f, indent=2)
        print(f"--- {tag}: n={summary['n_trades']} WR={summary['win_rate']} "
              f"PF={summary['profit_factor']} P&L={summary['total_pnl']} "
              f"rejects={summary['rejects']} "
              f"fresh={summary['coverage']['fresh_api_calls']}", flush=True)


if __name__ == '__main__':
    main()
