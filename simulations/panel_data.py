#!/usr/bin/env python3
"""Flow-overlay study panel data layer: enumerate missing (contract, day)
option-bar keys for DAILY S2b entries (Mon-Fri 10:00 ET), then one paced
Polygon fetch loop. Modeled exactly on variants_data.py (two-pass discipline).
NO Databento.

Pass 1 (--enumerate, network OFF): Coverage.get_day_bars patched to serve
cache hits and queue misses. Strategy = S2b structure entered every weekday;
MAX_CONCURRENT=20 and daily-loss halt disabled so no entry's keys are
suppressed by portfolio state (superset is safe).

Pass 2 (--fetch): paced loop (0.30s, 12s backoff on 429) over missing keys,
store ONLY on HTTP success. Restartable. Never prints the API key.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

import engine_v345 as eng
import sim_proposed as sp
import sim_mech as sm
import strategies_shortlist as sl
import scale_s2b as sc
import variants_data as vd

MISSES_FILE = os.path.join(HERE, 'panel_misses.json')
LOG_FILE = os.path.join(HERE, 'fetch_panel.log')


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


class SpyDaily(sc.SpyPutspreadDays):
    name = 'spy_putspread_daily'
    weekdays = (0, 1, 2, 3, 4)


def enumerate_misses():
    sm.Coverage.get_day_bars = vd.patched_get_day_bars
    sm.DAILY_LOSS_HALT = 1e9
    sm.MAX_CONCURRENT = 20
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)   # never called
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    sim = sl.ShortlistSim(polygon, tradier, iv_daily, slip, closes, regime,
                          sl.WINDOW_START, sl.WINDOW_END)
    sim.run([SpyDaily()])
    cov = dict(sim.coverage.counts)
    assert polygon.api_calls == 0 and sim.coverage.fresh_calls == 0
    out = sorted(vd.MISSES)
    est_min = len(out) / vd.CALLS_PER_MIN_EST
    payload = {'generated': datetime.now().isoformat(timespec='seconds'),
               'coverage': cov, 'n_misses': len(out),
               'n_contracts': len({o for o, _ in out}),
               'est_minutes_at_5cpm': round(est_min, 1),
               'est_hours_at_5cpm': round(est_min / 60, 2),
               'misses': out}
    with open(MISSES_FILE, 'w') as f:
        json.dump(payload, f)
    log(f"PANEL ENUMERATION DONE: {len(out)} contract-day misses "
        f"({payload['n_contracts']} contracts); coverage={cov}; "
        f"est @5/min = {payload['est_hours_at_5cpm']} h "
        f"({'OK' if est_min <= 720 else 'OVER 12h -> fallback to MWF panel'})")
    return payload


def fetch_misses(max_keys=None):
    with open(MISSES_FILE) as f:
        payload = json.load(f)
    misses = [tuple(x) for x in payload['misses']]
    if max_keys:
        misses = misses[:max_keys]
    dc = eng.get_disk_cache()

    def present(key):
        return (dc.src.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone()
                or dc.conn.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone())

    todo = [(o, d) for o, d in misses
            if not present(f"polygon:bars_{o}_{d}_{d}_5_minute")]
    todo.sort()
    log(f"PANEL FETCH START: {len(todo)} of {len(misses)} keys still missing")
    ok = empty = failed = 0
    t0 = time.time()
    failures = []
    for i, (occ, ds) in enumerate(todo, 1):
        url = (f"{eng.POLYGON_BASE_URL}/v2/aggs/ticker/O:{occ}"
               f"/range/5/minute/{ds}/{ds}?adjusted=true&sort=asc&limit=50000"
               f"&apiKey={eng.POLYGON_API_KEY}")
        data, status = None, None
        for attempt in range(3):
            data, status = vd._paced_request(url)
            if data is not None:
                break
            time.sleep(5)
        if data is None:
            failed += 1
            failures.append([occ, ds, str(status)])
            log(f"  FAIL {occ} {ds} status={status} (not cached)")
            continue
        bars = data.get('results', []) or []
        dc.put(f"polygon:bars_{occ}_{ds}_{ds}_5_minute", bars)
        if bars:
            ok += 1
        else:
            empty += 1
        if i % 50 == 0 or i == len(todo):
            rate = i / max(time.time() - t0, 1) * 60
            eta_h = (len(todo) - i) / max(rate, 0.01) / 60
            log(f"  {i}/{len(todo)} ok={ok} empty={empty} failed={failed} "
                f"rate={rate:.1f}/min eta={eta_h:.2f}h")
    log(f"PANEL FETCH DONE: ok={ok} empty={empty} failed={failed} "
        f"elapsed={(time.time()-t0)/60:.1f} min")
    if failures:
        with open(os.path.join(HERE, 'panel_fetch_failures.json'), 'w') as f:
            json.dump(failures, f, indent=1)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--enumerate', action='store_true')
    ap.add_argument('--fetch', action='store_true')
    ap.add_argument('--max-keys', type=int, default=None)
    args = ap.parse_args()
    if args.enumerate:
        enumerate_misses()
    if args.fetch:
        fetch_misses(args.max_keys)
