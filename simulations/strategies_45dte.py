#!/usr/bin/env python3
"""45-DTE practitioner playbook validation (2026-06-11) — tastytrade-style
mechanics on the sim_mech/ShortlistSim harness. NO Databento; free Polygon only.

Arms (entered Mondays 10:00 ET; window 2025-03-03..2026-02-27, H1<=2025-08-28):
  P1 spy_45dte_putspread       short ~30d SPY put, wing $15 below, expiry
                               35-55 DTE (monthly preferred, else Friday
                               weekly nearest 45 DTE), gate: SPY IVR>=30
                               (60-session percentile of iv_daily, point-in-
                               time: prior session's value vs trailing 60).
                               Manage: TP 50% credit, stop 2x credit
                               (harness stop_mult=2.0 = buyback at 3x credit,
                               loss ~2x credit — same convention as S2b),
                               time exit 21 DTE.
  P2 spy_45dte_ic              same cycle, iron condor short ~20d both sides,
                               wings $15, TP 50% / 21 DTE, NO stop.
  P3 multi_underlying_45dte    P1 mechanics on the HIGHEST-IVR of SPY/QQQ/IWM
                               that Monday (>=30; skip if none). Width $15
                               SPY/QQQ, $5 IWM (price-scaled ~640 vs ~230).
  P4 spy_45dte_putspread_noivr P1 without the IVR gate (control).

Documented proxies / approximations (beyond the harness's standing ones):
  * Strikes snapped to the $5 grid for all 45-DTE structures (the s1b lesson:
    deep-OTM monthly SPY liquidity clusters on $5 multiples; $1-grid wings
    often have ZERO prints). Entry fill window widened to 60 min (s1b).
  * EVERY-OTHER-DAY bar sampling for the middle of the hold: option bars are
    fetched for the entry session, every 2nd session thereafter, and ALWAYS
    the 21-DTE boundary session + the next session (so the time exit resolves
    on the correct day). TP-50%/stop checks therefore run on ~every-other-day
    marks; an exit that would have triggered on a skipped day resolves on the
    next fetched day. Authorized approximation, biases timing not direction.
  * Entries whose 21-DTE exit boundary falls past the data window are SKIPPED
    (cannot be observed): only 2026-02-23 (and later) — 47 usable Mondays.
  * IVR uses iv_daily (median whalestream alert IV, biased high in level —
    harmless for a self-referential percentile) with the sim_proposed 60-
    session machinery: hist = last 60 sessions strictly before entry date,
    cur = most recent of those, rank = 100*count(hist<cur)/60, needs >=20
    sessions of history (first 4 Mondays of the window have none -> gated
    arms skip, P4 still enters).

Modes:
  --gate-stats     print the Monday IVR table (no network)
  --enumerate      network-OFF two-pass miss enumeration for ALL arms in
                   priority order P1 -> P4 -> P2 -> P3; writes
                   misses_45dte.json incl. wall-clock estimate @5 calls/min
                   BEFORE any fetch; flags scope cuts if projected > 14 h.
  --fetch [--max-hours H]  paced free-Polygon fetch of the missing keys in
                   priority order (cut from the bottom if over budget).
  --run [--only tag ...]   replay all arms x {baseline, 2x costs} on $20k,
                   5% of current equity vs max loss, max 3 concurrent.
Artifacts: trades_45dte_<tag>.csv, summary_45dte_<tag>.json, fetch_45dte.log.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

import engine_v345 as eng
import sim_proposed as sp
import sim_mech as sm
import strategies_shortlist as sl
import scale_s2b as sc            # sets sm.ACCOUNT_START = 20_000 on import
import underlying_bars as ub

WINDOW_START = sl.WINDOW_START          # 2025-03-03
WINDOW_END = sl.WINDOW_END              # 2026-02-27
H1_END = sl.H1_END                      # 2025-08-28
MISSES_FILE = os.path.join(HERE, 'misses_45dte.json')
LOG_FILE = os.path.join(HERE, 'fetch_45dte.log')
PACE = 0.30
CALLS_PER_MIN_EST = 5.0
MAX_FETCH_HOURS = 14.0
IVR_GATE = 30.0
WIDTHS = {'SPY': 15.0, 'QQQ': 15.0, 'IWM': 5.0}
P3_SYMBOLS = ['SPY', 'QQQ', 'IWM']


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')


# ------------------------------------------------------------------ IV rank
def iv_rank_60(iv_daily, sym, date_str):
    """sim_proposed 60-session percentile, point-in-time (prior session's
    median alert IV ranked vs trailing 60 sessions strictly before date)."""
    hist_dates = [d for d in sorted(iv_daily.get(sym, {})) if d < date_str][-60:]
    if len(hist_dates) < sp.MIN_IVR_HISTORY:        # 20
        return None
    hist = [iv_daily[sym][d] for d in hist_dates]
    cur = hist[-1]
    return 100.0 * sum(1 for h in hist if h < cur) / len(hist)


# ------------------------------------------------------------ expiry picking
def pick_45dte_expiry(date_str):
    """All holiday-adjusted Fridays with DTE 35-55; prefer the monthly
    (3rd Friday) if any is in range, else the Friday closest to 45 DTE.
    Returns (expiration, dte) or (None, None)."""
    d = datetime.strptime(date_str, '%Y-%m-%d')
    cands = []
    cur = d + timedelta(days=35)
    while (cur - d).days <= 55:
        if cur.weekday() == 4:
            e = cur
            while e.strftime('%Y-%m-%d') in eng.US_MARKET_HOLIDAYS:
                e -= timedelta(days=1)
            cands.append(e)
        cur += timedelta(days=1)
    if not cands:
        return None, None
    monthlies = set()
    for k in range(0, 4):
        y, m = d.year + (d.month + k - 1) // 12, (d.month + k - 1) % 12 + 1
        monthlies.add(sl.third_friday(y, m).strftime('%Y-%m-%d'))
    m_in = [e for e in cands if e.strftime('%Y-%m-%d') in monthlies]
    pool = m_in if m_in else cands
    best = min(pool, key=lambda e: abs((e - d).days - 45))
    return best.strftime('%Y-%m-%d'), (best - d).days


def exit_boundary_sessions(expiration):
    """(first session with DTE<=21, the session after) — capped to window."""
    tgt = (datetime.strptime(expiration, '%Y-%m-%d')
           - timedelta(days=21)).strftime('%Y-%m-%d')
    sess = ub.trading_sessions(WINDOW_START, WINDOW_END)
    after = [s for s in sess if s >= tgt]
    return after[:2]                       # may be [], [one], or [b, b+1]


# --------------------------------------------------------------- strategies
class P45Base(sm.MechStrategy):
    """Monday 10:00 ET entry; subclass sets structure/delta/gate."""
    symbols = ['SPY']
    structure = 'bull_put'
    short_delta = 0.30
    gated = True
    stop_mult = 2.0          # None for the condor
    iv_daily = None          # injected by run code

    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_monday = datetime.strptime(date, '%Y-%m-%d').weekday() == 0

    def pick_symbol(self, date):
        sym = self.symbols[0]
        if not self.gated:
            return sym
        r = iv_rank_60(self.iv_daily, sym, date)
        return sym if (r is not None and r >= IVR_GATE) else None

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_monday or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        date = dt.strftime('%Y-%m-%d')
        sym = self.pick_symbol(date)
        if sym is None or not bars_so_far.get(sym):
            return []
        exp, dte = pick_45dte_expiry(date)
        if exp is None:
            return []
        b = exit_boundary_sessions(exp)
        if not b:                      # 21-DTE exit unobservable in window
            return []
        o = sm.SpreadOrder(sym, self.structure, short_delta=self.short_delta,
                           width=WIDTHS[sym], dte_target=45, tp_pct=0.50,
                           stop_mult=self.stop_mult, time_exit_dte=21,
                           tag=self.name)
        return [sl._mk(o, expiry45=(exp, dte), fill_window_min=60,
                       eod_sample=True)]


class Spy45PutSpread(P45Base):
    name = 'p1_spy_45dte_putspread'


class Spy45PutSpreadNoIvr(P45Base):
    name = 'p4_spy_45dte_putspread_noivr'
    gated = False


class Spy45IronCondor(P45Base):
    name = 'p2_spy_45dte_ic'
    structure = 'iron_condor'
    short_delta = 0.20
    stop_mult = None         # condor max-loss is the stop (house style)


class Multi45PutSpread(P45Base):
    name = 'p3_multi_underlying_45dte'
    symbols = P3_SYMBOLS

    def pick_symbol(self, date):
        best_sym, best_r = None, IVR_GATE
        for s in self.symbols:
            r = iv_rank_60(self.iv_daily, s, date)
            if r is not None and r >= best_r:
                best_sym, best_r = s, r
        return best_sym


STRATEGIES = {c.name: c for c in
              (Spy45PutSpread, Spy45PutSpreadNoIvr, Spy45IronCondor,
               Multi45PutSpread)}
PRIORITY = ['p1_spy_45dte_putspread', 'p4_spy_45dte_putspread_noivr',
            'p2_spy_45dte_ic', 'p3_multi_underlying_45dte']


# --------------------------------------------------------------------- sim
class Sim45(sc.EquitySim):
    """EquitySim ($20k, % of current equity) + 45-DTE leg construction on a
    $5-snapped grid + every-other-day sampled bar fetching."""

    def build_legs(self, order, date_str, spot, now_et):
        exp_dte = getattr(order, 'expiry45', None)
        if exp_dte is None:
            return super().build_legs(order, date_str, spot, now_et)
        expiration, dte = exp_dte
        strikes = [k for k in self.strike_grid(order.symbol, expiration, spot)
                   if k % 5 == 0]
        if not strikes:
            return None, 'no_strikes'
        sigma = self.iv_for(order.symbol, date_str)
        if not sigma:
            return None, 'no_iv'
        dte_f = float(dte)
        sym = order.symbol

        def leg(opt, strike, side):
            return {'occ': eng.build_occ_symbol(sym, expiration, opt, strike),
                    'side': side, 'strike': strike, 'opt': opt,
                    'expiration': expiration}

        legs, deltas, widths = [], {}, []
        sides = (('P', 'put_short'),) if order.structure == 'bull_put' else \
                (('P', 'put_short'), ('C', 'call_short'))
        for opt, dkey in sides:
            ks, d = self._short_by_delta(strikes, spot, opt,
                                         order.short_delta, dte_f, sigma)
            if ks is None:
                return None, 'no_short_strike'
            pool = [k for k in strikes if (k < ks if opt == 'P' else k > ks)]
            tgt = ks - order.width if opt == 'P' else ks + order.width
            kl = eng.find_nearest_strike(tgt, pool)
            if kl is None:
                return None, 'no_long_strike'
            legs += [leg(opt, ks, -1), leg(opt, kl, +1)]
            deltas[dkey] = d
            widths.append(abs(ks - kl))
        if any(w <= 0 for w in widths):
            return None, 'zero_width'
        meta = {'is_credit': True, 'max_width': max(widths),
                'expiration': expiration, 'dte': dte, 'deltas': deltas,
                'sigma': sigma}
        return legs, meta

    def aligned_bars(self, legs, start_date, end_date):
        o = self._cur_order
        if o is None or not getattr(o, 'eod_sample', False):
            return super().aligned_bars(legs, start_date, end_date)
        expiration = legs[0]['expiration']
        bnd = exit_boundary_sessions(expiration)
        end = min(end_date, WINDOW_END, bnd[-1] if bnd else WINDOW_END)
        all_sess = ub.trading_sessions(start_date, end)
        mid = [s for s in all_sess if not bnd or s < bnd[0]]
        keep = set(mid[::2]) | {start_date} | set(bnd)
        sessions = [s for s in all_sess if s in keep]
        # ---- same per-leg fetch/intersect as MechSim.aligned_bars
        per_leg = []
        for l in legs:
            by_ts = {}
            for ds in sessions:
                if ds > l['expiration']:
                    continue
                for b in self.coverage.get_day_bars(l['occ'], ds):
                    by_ts[b['t']] = b['c']
            per_leg.append(by_ts)
        common = set(per_leg[0])
        for m in per_leg[1:]:
            common &= set(m)
        out = []
        for ts in sorted(common):
            et = sm.ts_et(ts)
            if not (dtime(9, 30) <= et.time() < dtime(16, 5)):
                continue
            out.append({'ts': ts, 'date': et.strftime('%Y-%m-%d'),
                        'time_et': et.strftime('%H:%M'),
                        'value': sum(l['side'] * m[ts]
                                     for l, m in zip(legs, per_leg))})
        return out


# ----------------------------------------------------------------- gate stats
def gate_stats(iv_daily, verbose=True):
    sess = ub.trading_sessions(WINDOW_START, WINDOW_END)
    mondays = [s for s in sess
               if datetime.strptime(s, '%Y-%m-%d').weekday() == 0]
    rows = []
    for m in mondays:
        r = {s: iv_rank_60(iv_daily, s, m) for s in P3_SYMBOLS}
        exp, dte = pick_45dte_expiry(m)
        usable = bool(exit_boundary_sessions(exp)) if exp else False
        best = max(((v, s) for s, v in r.items() if v is not None),
                   default=(None, None))
        p3 = best[1] if (best[0] is not None and best[0] >= IVR_GATE) else None
        rows.append({'monday': m, 'ivr': r, 'exp': exp, 'dte': dte,
                     'usable': usable, 'p3_pick': p3,
                     'p1_pass': bool(r['SPY'] is not None
                                     and r['SPY'] >= IVR_GATE)})
    if verbose:
        for x in rows:
            f = lambda v: f"{v:5.1f}" if v is not None else "  n/a"
            print(x['monday'], 'SPY', f(x['ivr']['SPY']),
                  'QQQ', f(x['ivr']['QQQ']), 'IWM', f(x['ivr']['IWM']),
                  '| exp', x['exp'], f"dte={x['dte']}",
                  '| P1', 'PASS' if x['p1_pass'] else 'skip',
                  '| P3', x['p3_pick'] or '-',
                  '' if x['usable'] else '| DROP(exit>window)')
        u = [x for x in rows if x['usable']]
        h1 = [x for x in u if x['monday'] <= H1_END]
        h2 = [x for x in u if x['monday'] > H1_END]
        print(f"\nusable Mondays {len(u)} (H1 {len(h1)} / H2 {len(h2)}); "
              f"P1 gate passes {sum(x['p1_pass'] for x in u)} "
              f"(H1 {sum(x['p1_pass'] for x in h1)} / "
              f"H2 {sum(x['p1_pass'] for x in h2)}); "
              f"P3 enters {sum(1 for x in u if x['p3_pick'])} "
              f"(H1 {sum(1 for x in h1 if x['p3_pick'])} / "
              f"H2 {sum(1 for x in h2 if x['p3_pick'])}) "
              f"by sym { {s: sum(1 for x in u if x['p3_pick']==s) for s in P3_SYMBOLS} }")
    return rows


# ---------------------------------------------------------------- enumerate
MISSES = []          # ordered (occ, ds, first_needing_strategy)
_MISS_SET = set()


def patched_get_day_bars(self, occ, ds):
    k = (occ, ds)
    if k in self.seen:
        return self.seen[k][1]
    key = f"polygon:bars_{occ}_{ds}_{ds}_5_minute"
    tier = self._probe(key)
    if tier is None:
        if k not in _MISS_SET:
            _MISS_SET.add(k)
            MISSES.append((occ, ds, CURRENT_STRAT[0]))
        self.counts['miss_queued'] += 1
        self.seen[k] = ('miss', [])
        return []
    bars = self.dc.get(key) or []
    self.counts[tier] += 1
    self.seen[k] = (tier, bars)
    return bars


CURRENT_STRAT = ['?']


def enumerate_misses():
    sm.Coverage.get_day_bars = patched_get_day_bars
    sm.DAILY_LOSS_HALT = 1e9          # superset enumeration
    prev_cap = sm.MAX_CONCURRENT
    sm.MAX_CONCURRENT = 99            # 45-DTE holds would jam slots with no
    try:                              # bars (exits collapse to expiration)
        iv_daily, slip = sm.load_iv_slip()
        P45Base.iv_daily = iv_daily
        closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
        regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
        polygon = eng.PolygonClient(eng.POLYGON_API_KEY)      # never called
        tradier = eng.TradierClient(eng.TRADIER_API_KEY)
        per_strat = {}
        for name in PRIORITY:
            CURRENT_STRAT[0] = name
            before = len(MISSES)
            sim = Sim45(polygon, tradier, iv_daily, slip, closes, regime,
                        WINDOW_START, WINDOW_END)
            sim.run([STRATEGIES[name]()])
            cov = dict(sim.coverage.counts)
            per_strat[name] = {'requested': sum(cov.values()),
                               'new_misses': len(MISSES) - before, **cov}
            log(f"enumerated {name}: requested={sum(cov.values())} "
                f"new_misses={len(MISSES) - before} cum={len(MISSES)}")
            assert polygon.api_calls == 0 and sim.coverage.fresh_calls == 0
    finally:
        sm.MAX_CONCURRENT = prev_cap
    est_min = len(MISSES) / CALLS_PER_MIN_EST
    cum = 0
    cuts = {}
    for name in PRIORITY:
        cum += sum(1 for *_x, s in MISSES if s == name)
        cuts[name] = {'cum_keys': cum,
                      'cum_hours_at_5cpm': round(cum / CALLS_PER_MIN_EST / 60, 2)}
    payload = {'generated': datetime.now().isoformat(timespec='seconds'),
               'per_strat': per_strat, 'n_misses': len(MISSES),
               'n_contracts': len({o for o, _d, _s in MISSES}),
               'est_minutes_at_5cpm': round(est_min, 1),
               'est_hours_at_5cpm': round(est_min / 60, 2),
               'cumulative_by_priority': cuts,
               'misses': MISSES}
    with open(MISSES_FILE, 'w') as f:
        json.dump(payload, f)
    over = est_min / 60 > MAX_FETCH_HOURS
    verdict = (f"OVER {MAX_FETCH_HOURS}h cap -> CUT per priority "
               + '->'.join(PRIORITY)) if over else f"within {MAX_FETCH_HOURS}h cap"
    log(f"ENUMERATION DONE: {payload['n_misses']} contract-day misses "
        f"({payload['n_contracts']} contracts). WALL-CLOCK ESTIMATE @ ~5 "
        f"successful calls/min: {payload['est_minutes_at_5cpm']} min = "
        f"{payload['est_hours_at_5cpm']} h ({verdict})")
    log(f"cumulative by priority: {json.dumps(cuts)}")
    return payload


# -------------------------------------------------------------------- fetch
_last = [0.0]


def _paced_request(url):
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


def fetch_misses(max_hours=MAX_FETCH_HOURS):
    with open(MISSES_FILE) as f:
        payload = json.load(f)
    misses = [tuple(x) for x in payload['misses']]   # already priority-ordered
    dc = eng.get_disk_cache()

    def present(key):
        return (dc.src.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone()
                or dc.conn.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone())

    todo = [(o, d, s) for o, d, s in misses
            if not present(f"polygon:bars_{o}_{d}_{d}_5_minute")]
    log(f"FETCH START: {len(todo)} of {len(misses)} keys still missing; "
        f"ETA @5/min ~ {len(todo)/5/60:.2f} h (cap {max_hours} h; "
        f"priority order {'->'.join(PRIORITY)}, cut from the bottom)")
    ok = empty = failed = 0
    t0 = time.time()
    failures = []
    for i, (occ, ds, strat) in enumerate(todo, 1):
        if (time.time() - t0) / 3600 > max_hours:
            log(f"!! WALL-CLOCK CAP {max_hours}h hit at {i-1}/{len(todo)} "
                f"(next would be {strat}); remaining keys cut")
            break
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
        if i % 50 == 0 or i == len(todo):
            rate = i / max(time.time() - t0, 1) * 60
            eta_h = (len(todo) - i) / max(rate, 0.01) / 60
            log(f"  {i}/{len(todo)} ok={ok} empty={empty} failed={failed} "
                f"rate={rate:.1f}/min eta={eta_h:.2f}h")
    log(f"FETCH DONE: ok={ok} empty={empty} failed={failed} "
        f"elapsed={(time.time()-t0)/60:.1f} min")
    if failures:
        with open(os.path.join(HERE, 'fetch_45dte_failures.json'), 'w') as f:
            json.dump(failures, f, indent=1)
        log(f"  {len(failures)} failures -> fetch_45dte_failures.json")


# --------------------------------------------------------------------- runs
def monthly_pnl(sim):
    out = defaultdict(float)
    for p in sim.trades:
        out[p['exit_bar']['date'][:7]] += p['pnl']
    return {k: round(v, 2) for k, v in sorted(out.items())}


def run_all(only=None, allow_fresh=200):
    iv_daily, slip = sm.load_iv_slip()
    P45Base.iv_daily = iv_daily
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    for name in PRIORITY:
        for ss, suf in ((1.0, ''), (2.0, '_slip2x')):
            tag = f"{name}{suf}"
            if only and tag not in only:
                continue
            out_path = os.path.join(HERE, f'summary_45dte_{tag}.json')
            if os.path.exists(out_path):
                print(f'skip {tag}: exists', flush=True)
                continue
            sm.MAX_FRESH_API_CALLS = allow_fresh
            print(f"\n=== {tag} ===", flush=True)
            sim = Sim45(polygon, tradier, iv_daily, slip, closes, regime,
                        WINDOW_START, WINDOW_END,
                        risk_pct_equity=0.05, slip_scale=ss)
            try:
                base = sim.run([STRATEGIES[name]()])
            except sm.BudgetExceeded as e:
                print(f"!! ABORT {tag}: {e}", flush=True)
                base = sim.summarize()
                base['aborted'] = str(e)
            ext = sc.extended_metrics(sim)
            summary = {'tag': tag, 'account': sc.ACCOUNT,
                       'risk_pct_equity': 0.05, 'slip_scale': ss,
                       'max_concurrent': sm.MAX_CONCURRENT,
                       'window': [WINDOW_START, WINDOW_END], 'h1_end': H1_END,
                       **ext,
                       'monthly_pnl': monthly_pnl(sim),
                       'by_exit_reason': base['by_exit_reason'],
                       'rejects': base['rejects'],
                       'coverage': base['coverage']}
            if 'aborted' in base:
                summary['aborted'] = base['aborted']
            sim.dump_trades(os.path.join(HERE, f'trades_45dte_{tag}.csv'))
            with open(out_path, 'w') as f:
                json.dump(summary, f, indent=2)
            print(f"--- {tag}: n={ext['full']['n']} WR={ext['full']['wr']} "
                  f"PF={ext['full']['pf']} (H1 {ext['h1']['pf']} / "
                  f"H2 {ext['h2']['pf']}) P&L={ext['total_pnl']} "
                  f"(${ext['pnl_per_month']}/mo) "
                  f"maxDD=${ext['max_drawdown_dollars']} "
                  f"fresh={base['coverage']['fresh_api_calls']}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gate-stats', action='store_true')
    ap.add_argument('--enumerate', action='store_true')
    ap.add_argument('--fetch', action='store_true')
    ap.add_argument('--max-hours', type=float, default=MAX_FETCH_HOURS)
    ap.add_argument('--run', action='store_true')
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--allow-fresh', type=int, default=200)
    args = ap.parse_args()
    if args.gate_stats:
        iv_daily, _ = sm.load_iv_slip()
        gate_stats(iv_daily)
    if args.enumerate:
        enumerate_misses()
    if args.fetch:
        fetch_misses(args.max_hours)
    if args.run:
        run_all(args.only, args.allow_fresh)


if __name__ == '__main__':
    main()
