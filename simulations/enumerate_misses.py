#!/usr/bin/env python3
"""Miss enumeration: run every shortlist strategy through the harness with a
NO-NETWORK fetcher that queues cache misses instead of pulling, and emit the
union of (contract, ET-day) needs to shortlist_misses.json.

Coverage.get_day_bars is patched to: serve archive/overlay hits normally,
record a miss and return [] otherwise. Returning [] cannot under-enumerate:
aligned_bars() requests every (leg, session) key for a position's full window
(entry-day-only honored) BEFORE any fill logic runs, and entries are clock-
driven, so the request set is independent of fills/P&L (no-trade paths only
remove daily-halt suppression, which would otherwise SHRINK the set).
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

MISSES = set()


def patched_get_day_bars(self, occ, ds):
    k = (occ, ds)
    if k in self.seen:
        return self.seen[k][1]
    key = f"polygon:bars_{occ}_{ds}_{ds}_5_minute"
    tier = self._probe(key)
    if tier is None:
        MISSES.add((occ, ds))
        self.counts['miss_queued'] += 1
        self.seen[k] = ('miss', [])
        return []
    bars = self.dc.get(key) or []
    self.counts[tier] += 1
    self.seen[k] = (tier, bars)
    return bars


sm.Coverage.get_day_bars = patched_get_day_bars

STRATS = ['spy_putspread_weekly', 'spy_putspread_weekly_managed',
          'spy_condor_weekly', 'spy_condor_monthly', 'spy_bfly_monthly',
          'spy_0dte_putspread', 'qqq_0dte_putspread']


def main():
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)   # never called
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    per_strat = {}
    for name in STRATS:
        before = len(MISSES)
        sim = sl.ShortlistSim(polygon, tradier, iv_daily, slip, closes, regime,
                              sl.WINDOW_START, sl.WINDOW_END)
        sim.run([sl.STRATEGIES[name]()])
        cov = sim.coverage.counts
        new = len(MISSES) - before
        per_strat[name] = {'requested': sum(cov.values()), 'new_misses': new,
                           **{k: cov[k] for k in sorted(cov)}}
        print(f"== {name}: requested={sum(cov.values())} new_misses={new} "
              f"cum_misses={len(MISSES)}", flush=True)
        assert polygon.api_calls == 0 and sim.coverage.fresh_calls == 0
    out = sorted(MISSES)
    with open(os.path.join(HERE, 'shortlist_misses.json'), 'w') as f:
        json.dump(out, f)
    print(json.dumps(per_strat, indent=1))
    print(f"TOTAL union misses: {len(out)} contract-days, "
          f"{len({o for o, _ in out})} distinct contracts")


if __name__ == '__main__':
    main()
