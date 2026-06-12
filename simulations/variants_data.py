#!/usr/bin/env python3
"""PHASE B data layer for the S2b scaling study: two-pass miss-enumerate +
ONE paced Polygon fetch loop. NO Databento.

Pass 1 (--enumerate, network OFF): patch Coverage.get_day_bars to serve
archive/overlay hits and queue misses (returning []). Run V2 (SPY Mon+Wed+Fri
managed) and V3 (QQQ Mon managed) through the harness; V1 wed/fri request sets
are exact subsets of V2 (leg construction depends only on date/spot/IV, not on
portfolio state), and V4 = V2 + V3. Daily-loss halt is disabled during
enumeration so cached-Monday losses can't suppress Wed/Fri entry enumeration
(superset is safe; the real runs keep the halt). Writes variant_misses.json
incl. a wall-clock estimate at ~5 successful calls/min.

Pass 2 (--fetch): one paced loop over the missing (contract, ET-day) keys.
0.30s pacing, 12s backoff on 429; stores ONLY on HTTP success (genuinely-empty
days stored as [] — transient errors are retried then recorded as failed
WITHOUT caching, so they cannot poison the overlay). Restartable: keys already
present are skipped. Progress + ETA appended to fetch_variants.log every 25
keys. Never prints the API key.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

import engine_v345 as eng
import sim_proposed as sp
import sim_mech as sm
import strategies_shortlist as sl
import scale_s2b as sc

MISSES_FILE = os.path.join(HERE, 'variant_misses.json')
LOG_FILE = os.path.join(HERE, 'fetch_variants.log')
PACE = 0.30
CALLS_PER_MIN_EST = 5.0


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


# ------------------------------------------------------------------ enumerate
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


def enumerate_misses():
    sm.Coverage.get_day_bars = patched_get_day_bars
    sm.DAILY_LOSS_HALT = 1e9            # superset enumeration (see docstring)
    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)   # never called
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    per_strat = {}
    # priority order: V2 union first, then V3 (cut from the bottom if needed)
    for label, classes in [('v2_mwf', [sc.SpyMWF]), ('v3_qqq_mon', [sc.QqqMon])]:
        before = len(MISSES)
        sim = sl.ShortlistSim(polygon, tradier, iv_daily, slip, closes, regime,
                              sl.WINDOW_START, sl.WINDOW_END)
        sim.run([c() for c in classes])
        cov = dict(sim.coverage.counts)
        per_strat[label] = {'requested': sum(cov.values()),
                            'new_misses': len(MISSES) - before, **cov}
        log(f"enumerated {label}: requested={sum(cov.values())} "
            f"new_misses={len(MISSES) - before} cum={len(MISSES)}")
        assert polygon.api_calls == 0 and sim.coverage.fresh_calls == 0
    out = sorted(MISSES)
    est_min = len(out) / CALLS_PER_MIN_EST
    payload = {'generated': datetime.now().isoformat(timespec='seconds'),
               'per_strat': per_strat, 'n_misses': len(out),
               'n_contracts': len({o for o, _ in out}),
               'est_minutes_at_5cpm': round(est_min, 1),
               'est_hours_at_5cpm': round(est_min / 60, 2),
               'misses': out}
    with open(MISSES_FILE, 'w') as f:
        json.dump(payload, f)
    log(f"ENUMERATION DONE: {len(out)} contract-day misses "
        f"({payload['n_contracts']} contracts). "
        f"WALL-CLOCK ESTIMATE @ ~5 successful calls/min: "
        f"{payload['est_minutes_at_5cpm']} min = {payload['est_hours_at_5cpm']} h "
        f"(12h cap -> {'OK' if est_min <= 720 else 'CUT FROM BOTTOM'})")
    return payload


# ---------------------------------------------------------------------- fetch
_last = [0.0]


def _paced_request(url):
    """Returns (json_or_None, http_status). Sleeps 12s + retries on 429."""
    while True:
        wait = PACE - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode()), 200
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(12)
                continue
            return None, e.code
        except Exception as e:
            return None, f'EXC:{type(e).__name__}'


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
    # priority: V2 union (SPY) first, then V3 (QQQ) — cut from the bottom
    todo.sort(key=lambda x: (not x[0].startswith('SPY'), x[0], x[1]))
    log(f"FETCH START: {len(todo)} of {len(misses)} keys still missing; "
        f"ETA @5/min ~ {len(todo)/5/60:.2f} h")
    ok = empty = failed = 0
    t0 = time.time()
    failures = []
    for i, (occ, ds) in enumerate(todo, 1):
        url = (f"{eng.POLYGON_BASE_URL}/v2/aggs/ticker/O:{occ}"
               f"/range/5/minute/{ds}/{ds}?adjusted=true&sort=asc&limit=50000"
               f"&apiKey={eng.POLYGON_API_KEY}")
        data, status = None, None
        for attempt in range(3):
            data, status = _paced_request(url)
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
        if i % 25 == 0 or i == len(todo):
            rate = i / max(time.time() - t0, 1) * 60
            eta_h = (len(todo) - i) / max(rate, 0.01) / 60
            log(f"  {i}/{len(todo)} ok={ok} empty={empty} failed={failed} "
                f"rate={rate:.1f}/min eta={eta_h:.2f}h")
    log(f"FETCH DONE: ok={ok} empty={empty} failed={failed} "
        f"elapsed={(time.time()-t0)/60:.1f} min")
    if failures:
        with open(os.path.join(HERE, 'variant_fetch_failures.json'), 'w') as f:
            json.dump(failures, f, indent=1)
        log(f"  {len(failures)} failures written to variant_fetch_failures.json")


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
