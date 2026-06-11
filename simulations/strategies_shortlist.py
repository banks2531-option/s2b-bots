#!/usr/bin/env python3
"""Shortlist validation strategies on the sim_mech harness (2026-06-11).

Implements the research shortlist (research/2026-06-11-options-strategy-research.md)
as MechStrategy subclasses + a ShortlistSim(MechSim) subclass. sim_mech.py is
NOT modified; everything strategy-specific lives here:

  s1a spy_condor_weekly      Mon 11:00 ET, SPY IC, short ~20d put+call,
                             long ~5d wings ($15 cap if 5d unreachable on the
                             $1 grid), nearest weekly >=4 DTE, hold to expiry.
  s1b spy_condor_monthly     same structure, entered first session after the
                             monthly (3rd-Friday) expiration, targeting the
                             next monthly (~25-32 DTE). CNDR-style.
  s2a spy_putspread_weekly   Mon 10:00 ET, short SPY ~40d put + wing $10
                             below, weekly >=4 DTE, hold to expiry (PUT-analog).
  s2b ..._managed            same entry, TP 50%, stop 2.0x credit, time exit
                             1 DTE.
  s3  spy_bfly_monthly       monthly ATM iron butterfly (strike_offset=0 IC),
                             wings 5% OTM, hold to expiry. Comparison arm.
  s5  spy_0dte_putspread     daily 10:00 ET, SPY 0DTE bull put spread,
                             short ~12d, $3 wide, credit-only, EOD flatten.
  (s4 IV-RV filtered variants of s1a/s2a are POST-FILTERS on the unfiltered
   trade logs — entry-time-only filters, no re-simulation; see
   analyze_shortlist.py.)

ShortlistSim extensions (kept out of sim_mech.py):
  * wing_delta wings: long wings picked by |BS delta| ~ wing_delta (5d),
    falling back to short -/+ wing_cap dollars if no candidate on the grid.
  * monthly expirations: order attr monthly=True -> next 3rd-Friday with
    DTE >= 21 (holiday step-back, e.g. Good-Friday weeks). Monthly strikes are
    snapped to the $5 grid and the entry fill window widened to 60 min:
    verified 2025-06-23 that $1-grid deep-OTM monthly wings often have ZERO
    prints (SPY250718C647 0 bars) while the $5-multiple neighbors trade all
    day (P555 65 bars, C645 20 bars, 4-leg aligned prints at 11:20/11:25) —
    deep-OTM monthly SPY liquidity clusters on $5 multiples.
  * entry_day_only: hold-to-expiry orders (no TP/stop/time/EOD triggers after
    entry day) fetch option bars for the ENTRY DAY ONLY; resolve_exit then
    settles intrinsically at expiration exactly as it would with full-life
    bars present (no trigger can fire), saving ~80% of the API budget.
  * slip_scale: multiplies the empirical per-leg half-spread (cost stress 2x).
  * premium_slip_pct: replaces the empirical half-spread with a round-trip
    cost = pct of the structure's entry mid premium (academic retail figure,
    5% effective spread => premium_slip_pct=0.05), split evenly entry/exit.

Budget: sim_mech.MAX_FRESH_API_CALLS is raised so that cumulative fresh calls
across all runs (persisted in budget_shortlist.json) stay <= 6000.

Run:  set OVERLAY_DB=api_cache_overlay_batch.db (set here as a default too)
      python strategies_shortlist.py            # full prescribed sequence
      python strategies_shortlist.py --only s1b_condor_monthly
      python strategies_shortlist.py --smoke    # June-2025 S1a logic check
Each run writes trades_sl_<tag>.csv + summary_sl_<tag>.json IMMEDIATELY
(checkpoint discipline); completed tags are skipped on restart.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, time as dtime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

import engine_v345 as eng
import sim_proposed as sp
import sim_mech as sm
import underlying_bars as ub

WINDOW_START = '2025-03-03'
WINDOW_END = '2026-02-27'
H1_END = '2025-08-28'
TOTAL_BUDGET = 6000
BUDGET_FILE = os.path.join(HERE, 'budget_shortlist.json')


# ------------------------------------------------------------ monthly expiry
def third_friday(year, month):
    d = datetime(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    while d.strftime('%Y-%m-%d') in eng.US_MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def next_monthly_expiry(date_str, min_dte=21):
    d = datetime.strptime(date_str, '%Y-%m-%d')
    y, m = d.year, d.month
    for _ in range(4):
        e = third_friday(y, m)
        if (e - d).days >= min_dte:
            return e.strftime('%Y-%m-%d'), (e - d).days
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return None, None


def monthly_entry_dates(start=WINDOW_START, end=WINDOW_END):
    """First trading session after each 3rd-Friday expiration, skipping
    cycles whose NEXT monthly expiry falls outside the data window."""
    sessions = ub.trading_sessions(start, end)
    out = []
    d = datetime.strptime(start, '%Y-%m-%d')
    y, m = d.year, d.month
    for _ in range(14):
        e3 = third_friday(y, m).strftime('%Y-%m-%d')
        entry = next((s for s in sessions if s > e3), None)
        if entry and entry <= end:
            exp, _ = next_monthly_expiry(entry, 21)
            if exp and exp <= end:          # drop the cycle expiring past the window
                out.append(entry)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return sorted(set(out))


# ----------------------------------------------------------------- strategies
def _mk(order, **extra):
    for k, v in extra.items():
        setattr(order, k, v)
    return order


class SpyCondorWeekly(sm.MechStrategy):
    """S1a: Mon 11:00 ET SPY IC, short ~20d, long ~5d wings (cap $15),
    weekly >=4 DTE, hold to expiry/settlement."""
    name = 'spy_condor_weekly'
    symbols = ['SPY']

    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_monday = datetime.strptime(date, '%Y-%m-%d').weekday() == 0

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_monday or dt.time() < dtime(11, 0):
            return []
        self.fired = True
        o = sm.SpreadOrder('SPY', 'iron_condor', short_delta=0.20, width=15.0,
                           dte_target=4, tag='s1a')
        return [_mk(o, wing_delta=0.05, wing_cap=15.0, entry_day_only=True)]


class SpyCondorMonthly(sm.MechStrategy):
    """S1b: first session after monthly expiration, 11:00 ET, same IC,
    next monthly (~25-32 DTE), hold to expiry. CNDR-analog."""
    name = 'spy_condor_monthly'
    symbols = ['SPY']
    entry_dates = None

    def on_session_start(self, date, ctx):
        if SpyCondorMonthly.entry_dates is None:
            SpyCondorMonthly.entry_dates = set(monthly_entry_dates())
        self.fired = False
        self.is_entry = date in SpyCondorMonthly.entry_dates

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_entry or dt.time() < dtime(11, 0):
            return []
        self.fired = True
        o = sm.SpreadOrder('SPY', 'iron_condor', short_delta=0.20, width=15.0,
                           dte_target=28, tag='s1b')
        return [_mk(o, wing_delta=0.05, wing_cap=15.0, monthly=True,
                    entry_day_only=True, fill_window_min=60)]


class SpyPutspreadWeekly(sm.MechStrategy):
    """S2a: Mon 10:00 ET, short SPY ~40d put, long wing $10 below,
    weekly >=4 DTE, hold to expiry (PUT-index analog)."""
    name = 'spy_putspread_weekly'
    symbols = ['SPY']
    managed = False

    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_monday = datetime.strptime(date, '%Y-%m-%d').weekday() == 0

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_monday or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        if self.managed:
            o = sm.SpreadOrder('SPY', 'bull_put', short_delta=0.40, width=10.0,
                               dte_target=4, tp_pct=0.50, stop_mult=2.0,
                               time_exit_dte=1, tag='s2b')
            return [o]                       # managed: needs full-life bars
        o = sm.SpreadOrder('SPY', 'bull_put', short_delta=0.40, width=10.0,
                           dte_target=4, tag='s2a')
        return [o]   # NOTE: s2a deliberately fetches full-life bars so the
                     # identical contracts are cached for s2b's managed exits.


class SpyPutspreadWeeklyManaged(SpyPutspreadWeekly):
    """S2b: same entry as S2a, TP 50%, stop 2.0x credit, time exit 1 DTE."""
    name = 'spy_putspread_weekly_managed'
    managed = True


class SpyBflyMonthly(sm.MechStrategy):
    """S3: monthly ATM iron butterfly, wings 5% OTM, hold to expiry.
    Same entry schedule as S1b for comparability. BFLY-analog."""
    name = 'spy_bfly_monthly'
    symbols = ['SPY']

    def on_session_start(self, date, ctx):
        if SpyCondorMonthly.entry_dates is None:
            SpyCondorMonthly.entry_dates = set(monthly_entry_dates())
        self.fired = False
        self.is_entry = date in SpyCondorMonthly.entry_dates

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_entry or dt.time() < dtime(11, 0):
            return []
        self.fired = True
        spot = bars_so_far['SPY'][-1]['c']
        width = max(1.0, round(spot * 0.05))
        o = sm.SpreadOrder('SPY', 'iron_condor', strike_offset=0.0,
                           width=width, dte_target=28, tag='s3')
        return [_mk(o, monthly=True, entry_day_only=True, fill_window_min=60)]


class Spy0dtePutspread(sm.MechStrategy):
    """S5: daily 10:00 ET, SPY 0DTE bull put spread, short ~12d, $3 wide,
    credit-only (harness rejects credit<=0), EOD flatten, no TP/stop."""
    name = 'spy_0dte_putspread'
    symbols = ['SPY']
    underlying = 'SPY'

    def on_session_start(self, date, ctx):
        self.fired = False

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        return [sm.SpreadOrder(self.underlying, 'bull_put', short_delta=0.12,
                               width=3.0, dte_target=0, eod_flatten=True,
                               tag='s5')]


class Qqq0dtePutspread(Spy0dtePutspread):
    """S5q: QQQ variant of S5 (budget-permitting extra)."""
    name = 'qqq_0dte_putspread'
    symbols = ['QQQ']
    underlying = 'QQQ'


# ------------------------------------------------------------- sim extensions
class ShortlistSim(sm.MechSim):
    def __init__(self, *a, slip_scale=1.0, premium_slip_pct=None, **kw):
        super().__init__(*a, **kw)
        self.slip_scale = slip_scale
        self.premium_slip_pct = premium_slip_pct
        self._cur_order = None

    # --- entry-day-only bar fetching for hold-to-expiry orders
    def aligned_bars(self, legs, start_date, end_date):
        o = self._cur_order
        if o is not None and getattr(o, 'entry_day_only', False):
            end_date = start_date
        return super().aligned_bars(legs, start_date, end_date)

    # --- monthly expiries + delta-targeted wings
    def build_legs(self, order, date_str, spot, now_et):
        monthly = getattr(order, 'monthly', False)
        wing_delta = getattr(order, 'wing_delta', None)
        if not monthly and wing_delta is None:
            return super().build_legs(order, date_str, spot, now_et)

        if monthly:
            expiration, dte = next_monthly_expiry(date_str, min_dte=21)
            if expiration is None:
                return None, 'no_monthly_expiry'
        else:
            expiration, dte = self.pick_expiration(date_str, order.dte_target)
        strikes = self.strike_grid(order.symbol, expiration, spot)
        if monthly:
            # deep-OTM monthly SPY liquidity clusters on $5 multiples; $1-grid
            # wings frequently have zero prints (see module docstring)
            strikes = [k for k in strikes if k % 5 == 0]
        if not strikes:
            return None, 'no_strikes'
        sigma = self.iv_for(order.symbol, date_str)
        if not sigma:
            return None, 'no_iv'
        dte_f = self._delta_dte(dte, now_et)
        sym = order.symbol
        cap = getattr(order, 'wing_cap', None) or 15.0

        def leg(opt, strike, side):
            return {'occ': eng.build_occ_symbol(sym, expiration, opt, strike),
                    'side': side, 'strike': strike, 'opt': opt,
                    'expiration': expiration}

        def short_strike(opt):
            if order.strike_offset is not None:
                tgt = (spot - order.strike_offset if opt == 'P'
                       else spot + order.strike_offset)
                return eng.find_nearest_strike(tgt, strikes), None

            return self._short_by_delta(strikes, spot, opt, order.short_delta,
                                        dte_f, sigma)

        def wing_strike(opt, ks):
            pool = [k for k in strikes if (k < ks if opt == 'P' else k > ks)]
            if not pool:
                return None
            if wing_delta is None:
                tgt = ks - order.width if opt == 'P' else ks + order.width
                return eng.find_nearest_strike(tgt, pool)
            best, best_err = None, 1e9
            for k in pool:
                d = sp.bs_delta(spot, k, dte_f, sigma, opt)
                if d is None:
                    continue
                err = abs(abs(d) - wing_delta)
                if err < best_err:
                    best, best_err = k, err
            if best is None:    # 5d unreachable on the grid -> $cap wings
                tgt = ks - cap if opt == 'P' else ks + cap
                best = eng.find_nearest_strike(tgt, pool)
            return best

        legs, deltas, widths = [], {}, []
        for opt, dkey in (('P', 'put_short'), ('C', 'call_short')):
            ks, d = short_strike(opt)
            if ks is None:
                return None, 'no_short_strike'
            kl = wing_strike(opt, ks)
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

    # --- try_enter: super()'s body + slip hook (slip_scale / premium pct).
    #     Kept verbatim except the slip_leg computation + _cur_order tracking.
    def try_enter(self, order, date_str, signal_ts, spot, strategy_name):
        self._cur_order = order
        try:
            if len(self.open_pos) >= sm.MAX_CONCURRENT:
                self.rejects['max_concurrent'] += 1
                return None
            now_et = sm.ts_et(signal_ts)
            legs, meta = self.build_legs(order, date_str, spot, now_et)
            if legs is None:
                self.rejects[meta] += 1
                return None
            bars = self.aligned_bars(legs, date_str, meta['expiration'])
            fill_win = getattr(order, 'fill_window_min', sm.ENTRY_FILL_WINDOW_MIN)
            cutoff = signal_ts + fill_win * 60_000
            entry_bar = next((b for b in bars
                              if b['date'] == date_str
                              and signal_ts <= b['ts'] <= cutoff), None)
            if entry_bar is None:
                self.rejects['no_pricing'] += 1
                return None
            # ---- slip hook (only deviation from sim_mech.MechSim.try_enter)
            if self.premium_slip_pct is not None:
                mid = abs(entry_bar['value'])
                slip_leg = (self.premium_slip_pct / 2.0) * mid / len(legs)
            else:
                slip_leg = self.slip.get(order.symbol,
                                         sm.SLIP_FLOOR) * self.slip_scale
            # ----
            slip_total = slip_leg * len(legs)
            pos = {'order': order, 'legs': legs, 'meta': meta, 'bars': bars,
                   'symbol': order.symbol, 'structure': order.structure,
                   'strategy': strategy_name, 'entry_date': date_str,
                   'entry_ts': entry_bar['ts'],
                   'entry_time_et': entry_bar['time_et'],
                   'spot': spot, 'slip_leg': slip_leg, 'sigma': meta['sigma'],
                   'expiration': meta['expiration'], 'dte': meta['dte'],
                   'deltas': meta['deltas'], 'n_aligned_bars': len(bars)}
            if meta['is_credit']:
                credit = -entry_bar['value'] - slip_total
                if credit <= 0:
                    self.rejects['credit_wiped'] += 1
                    return None
                if credit >= meta['max_width']:
                    self.rejects['credit_gt_width'] += 1
                    return None
                pos['credit'] = credit
                max_loss_per = (meta['max_width'] - credit) * 100.0
            else:
                debit = entry_bar['value'] + slip_total
                if debit <= 0:
                    self.rejects['debit_nonpositive'] += 1
                    return None
                pos['debit'] = debit
                max_loss_per = debit * 100.0
            qty = max(1, int(self.risk_dollars // max_loss_per))
            pos['qty'] = qty
            pos['max_loss_per'] = max_loss_per
            bar, cost, reason = self.resolve_exit(pos)
            pos['exit_bar'], pos['exit_cost'], pos['exit_reason'] = bar, cost, reason
            commission = sm.COMMISSION_PER_LEG_RT * len(legs) * qty
            if meta['is_credit']:
                pos['pnl'] = (pos['credit'] - cost) * qty * 100.0 - commission
            else:
                pos['pnl'] = (cost - pos['debit']) * qty * 100.0 - commission
            self.open_pos.append(pos)
            return pos
        finally:
            self._cur_order = None


# ------------------------------------------------------------------- running
STRATEGIES = {
    'spy_putspread_weekly': SpyPutspreadWeekly,
    'spy_putspread_weekly_managed': SpyPutspreadWeeklyManaged,
    'spy_condor_weekly': SpyCondorWeekly,
    'spy_condor_monthly': SpyCondorMonthly,
    'spy_bfly_monthly': SpyBflyMonthly,
    'spy_0dte_putspread': Spy0dtePutspread,
    'qqq_0dte_putspread': Qqq0dtePutspread,
}

# (tag, strategy, sim kwargs) in prescribed budget order; *_slip2x reruns are
# cache-free (identical contracts). s5 also gets the academic 5%-of-premium
# effective-spread scenario.
RUNS = [
    ('s2a_putspread_weekly',          'spy_putspread_weekly',         {}),
    ('s2a_putspread_weekly_slip2x',   'spy_putspread_weekly',         {'slip_scale': 2.0}),
    ('s2b_putspread_managed',         'spy_putspread_weekly_managed', {}),
    ('s2b_putspread_managed_slip2x',  'spy_putspread_weekly_managed', {'slip_scale': 2.0}),
    ('s1a_condor_weekly',             'spy_condor_weekly',            {}),
    ('s1a_condor_weekly_slip2x',      'spy_condor_weekly',            {'slip_scale': 2.0}),
    ('s1b_condor_monthly',            'spy_condor_monthly',           {}),
    ('s1b_condor_monthly_slip2x',     'spy_condor_monthly',           {'slip_scale': 2.0}),
    ('s3_bfly_monthly',               'spy_bfly_monthly',             {}),
    ('s3_bfly_monthly_slip2x',        'spy_bfly_monthly',             {'slip_scale': 2.0}),
    ('s5_0dte_putspread',             'spy_0dte_putspread',           {}),
    ('s5_0dte_putspread_slip2x',      'spy_0dte_putspread',           {'slip_scale': 2.0}),
    ('s5_0dte_putspread_slip5pct',    'spy_0dte_putspread',           {'premium_slip_pct': 0.05}),
]
QQQ_EXTRA = [
    ('s5q_0dte_putspread_qqq',        'qqq_0dte_putspread',           {}),
    ('s5q_0dte_putspread_qqq_slip2x', 'qqq_0dte_putspread',           {'slip_scale': 2.0}),
    ('s5q_0dte_putspread_qqq_slip5pct', 'qqq_0dte_putspread',         {'premium_slip_pct': 0.05}),
]
QQQ_MIN_REMAINING = 1500


def load_budget():
    if os.path.exists(BUDGET_FILE):
        with open(BUDGET_FILE) as f:
            return json.load(f)
    return {'used': 0, 'runs': {}}


def save_budget(b):
    with open(BUDGET_FILE, 'w') as f:
        json.dump(b, f, indent=2)


def run_one(tag, strat_name, sim_kwargs, polygon, tradier, iv_daily, slip,
            closes, regime, start, end, budget):
    remaining = TOTAL_BUDGET - budget['used']
    sm.MAX_FRESH_API_CALLS = max(0, remaining)
    print(f"\n=== {tag} ({strat_name}) {start}..{end} kwargs={sim_kwargs} "
          f"budget_remaining={remaining} ===", flush=True)
    sim = ShortlistSim(polygon, tradier, iv_daily, slip, closes, regime,
                       start, end, **sim_kwargs)
    strat = STRATEGIES[strat_name]()
    try:
        summary = sim.run([strat])
    except sm.BudgetExceeded as e:
        print(f"!! ABORT {tag}: API budget exceeded ({e})", flush=True)
        summary = sim.summarize()
        summary['aborted'] = str(e)
    summary['sim_kwargs'] = sim_kwargs
    summary['window'] = [start, end]
    sim.dump_trades(os.path.join(HERE, f'trades_sl_{tag}.csv'))
    with open(os.path.join(HERE, f'summary_sl_{tag}.json'), 'w') as f:
        json.dump(summary, f, indent=2)
    fresh = sim.coverage.fresh_calls
    budget['used'] += fresh
    budget['runs'][tag] = {'fresh_calls': fresh,
                           'n_trades': summary['n_trades'],
                           'pnl': summary['total_pnl'],
                           'pf': summary['profit_factor'],
                           'finished': datetime.now().isoformat(timespec='seconds')}
    save_budget(budget)
    print(f"--- {tag}: n={summary['n_trades']} WR={summary['win_rate']} "
          f"PF={summary['profit_factor']} P&L={summary['total_pnl']} "
          f"maxDD={summary['max_drawdown']} fresh_api={fresh} "
          f"(cum {budget['used']}/{TOTAL_BUDGET})", flush=True)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default=WINDOW_START)
    ap.add_argument('--end', default=WINDOW_END)
    ap.add_argument('--only', nargs='*', help='run only these tags')
    ap.add_argument('--smoke', action='store_true',
                    help='June-2025 S1a/S1b/S3 logic check (tiny)')
    ap.add_argument('--force', action='store_true',
                    help='rerun even if summary exists')
    ap.add_argument('--no-qqq', action='store_true')
    args = ap.parse_args()

    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    budget = load_budget()

    if args.smoke:
        for tag, name in [('smoke_s1a', 'spy_condor_weekly'),
                          ('smoke_s1b', 'spy_condor_monthly'),
                          ('smoke_s3', 'spy_bfly_monthly')]:
            run_one(tag, name, {}, polygon, tradier, iv_daily, slip, closes,
                    regime, '2025-06-01', '2025-06-30', budget)
        return

    runs = list(RUNS)
    for tag, name, kw in QQQ_EXTRA:
        runs.append((tag, name, kw))
    for tag, name, kw in runs:
        if args.only and tag not in args.only:
            continue
        if tag.startswith('s5q'):
            if args.no_qqq:
                continue
            if TOTAL_BUDGET - budget['used'] < QQQ_MIN_REMAINING:
                print(f"skip {tag}: only {TOTAL_BUDGET - budget['used']} calls "
                      f"left (< {QQQ_MIN_REMAINING})", flush=True)
                continue
        out = os.path.join(HERE, f'summary_sl_{tag}.json')
        if os.path.exists(out) and not args.force:
            print(f"skip {tag}: {out} exists", flush=True)
            continue
        run_one(tag, name, kw, polygon, tradier, iv_daily, slip, closes,
                regime, args.start, args.end, budget)
    print('\nALL DONE. budget:', json.dumps(load_budget()['runs'], indent=1),
          flush=True)


if __name__ == '__main__':
    main()
