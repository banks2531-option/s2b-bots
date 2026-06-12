"""Extended sizing frontier: what very steep drawdown tolerance buys.

Same machinery as scale_s2b phase A, risk levels 20-40% of equity per trade.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v345 as eng
import scale_s2b as sc
import sim_mech as sm
import sim_proposed as sp
import strategies_shortlist as sl


def main():
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    common = (polygon, tradier, iv_daily, slip, closes, regime)

    for pct, lbl in [(0.20, 'sz20'), (0.25, 'sz25'), (0.30, 'sz30'), (0.40, 'sz40')]:
        for ss, suf in [(1.0, ''), (2.0, '_slip2x')]:
            sc.run_one(f'{lbl}{suf}', [sl.SpyPutspreadWeeklyManaged], 3, pct, ss, *common)
    print('\nDONE', flush=True)


if __name__ == '__main__':
    main()
