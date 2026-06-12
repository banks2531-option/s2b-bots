#!/usr/bin/env python3
"""S2b scaling study (2026-06-11) — PHASE A sizing frontier + PHASE B variant runs.

PHASE A: rerun S2b (Mon 10:00 ET SPY ~40d put spread, $10 wing, >=4 DTE weekly,
TP 50%, stop 2.0x, time exit 1 DTE) on a $20k account at risk-per-trade
5% / 7.5% / 10% / 15% of CURRENT equity vs structure max loss (min 1 contract,
max 3 concurrent, harness daily-halt unchanged). Baseline + 2x costs.
ZERO new data: identical contracts to the validated s2b run (cache-hot).

PHASE B (after fetch_variants.py fills the cache): V1 Wed-only / Fri-only SPY,
V2 Mon+Wed+Fri SPY (max 3), V3 QQQ Mon-only ~40d $8 wide, V4 = V2 + QQQ Mon
(max 4 concurrent). Same management. Run at $20k 5%-equity sizing,
baseline + 2x costs.

Sizing semantics: qty = max(1, int((risk_pct * current_balance) // max_loss)).
NOTE deliberate deviation from the validated baseline (fixed % of STARTING
$10k): the study question is compounding extraction on $20k, so risk is
recomputed from realized equity at entry time.

Each run writes trades_scale_<tag>.csv + summary_scale_<tag>.json immediately.
Usage:
    python scale_s2b.py --phase a
    python scale_s2b.py --phase b [--only v1_wed ...]
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

import engine_v345 as eng
import sim_proposed as sp
import sim_mech as sm
import strategies_shortlist as sl

ACCOUNT = 20_000.0
WINDOW_START = sl.WINDOW_START
WINDOW_END = sl.WINDOW_END
H1_END = sl.H1_END
MONTHS = 11.86          # (2026-02-27 - 2025-03-03) = 361 days / 30.44

sm.ACCOUNT_START = ACCOUNT          # module global read at sim-construction time


# --------------------------------------------------------------- equity sizing
class EquitySim(sl.ShortlistSim):
    """% of CURRENT realized equity risk sizing (vs structure max loss)."""

    def __init__(self, *a, risk_pct_equity=0.05, **kw):
        self._risk_pct_equity = risk_pct_equity
        super().__init__(*a, **kw)

    @property
    def risk_dollars(self):
        return self._risk_pct_equity * self.balance

    @risk_dollars.setter
    def risk_dollars(self, v):           # base __init__ assigns a fixed value;
        pass                             # equity sizing supersedes it


# ----------------------------------------------------------- PHASE B strategies
class SpyPutspreadDays(sm.MechStrategy):
    """S2b entry/management on a configurable set of weekdays."""
    name = 'spy_putspread_days'
    symbols = ['SPY']
    weekdays = (0,)                      # 0=Mon, 2=Wed, 4=Fri

    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_entry = datetime.strptime(date, '%Y-%m-%d').weekday() in self.weekdays

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_entry or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        return [sm.SpreadOrder('SPY', 'bull_put', short_delta=0.40, width=10.0,
                               dte_target=4, tp_pct=0.50, stop_mult=2.0,
                               time_exit_dte=1, tag='s2b_var')]


class SpyWed(SpyPutspreadDays):
    name = 'spy_putspread_wed'
    weekdays = (2,)


class SpyFri(SpyPutspreadDays):
    name = 'spy_putspread_fri'
    weekdays = (4,)


class SpyMWF(SpyPutspreadDays):
    name = 'spy_putspread_mwf'
    weekdays = (0, 2, 4)


class QqqMon(sm.MechStrategy):
    """V3: QQQ Mon 10:00 ET, short ~40d put, $8 wide, same management."""
    name = 'qqq_putspread_mon'
    symbols = ['QQQ']

    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_entry = datetime.strptime(date, '%Y-%m-%d').weekday() == 0

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_entry or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        return [sm.SpreadOrder('QQQ', 'bull_put', short_delta=0.40, width=8.0,
                               dte_target=4, tp_pct=0.50, stop_mult=2.0,
                               time_exit_dte=1, tag='s2b_qqq')]


VARIANTS = {
    # tag -> (strategy classes, max_concurrent)
    'v1_wed': ([SpyWed], 3),
    'v1_fri': ([SpyFri], 3),
    'v2_mwf': ([SpyMWF], 3),
    'v3_qqq_mon': ([QqqMon], 3),
    'v4_mwf_plus_qqq': ([SpyMWF, QqqMon], 4),
}


# ------------------------------------------------------------------ metrics
def iso_week(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d').isocalendar()
    return f"{d[0]}-W{d[1]:02d}"


def trade_rows(sim):
    rows = []
    for p in sim.trades:
        rows.append({'entry_date': p['entry_date'],
                     'exit_date': p['exit_bar']['date'],
                     'exit_ts': p['exit_bar']['ts'],
                     'strategy': p['strategy'], 'symbol': p['symbol'],
                     'qty': p['qty'], 'pnl': round(p['pnl'], 2),
                     'exit_reason': p['exit_reason']})
    rows.sort(key=lambda r: (r['exit_date'], r['exit_ts']))
    return rows


def seg_stats(rows):
    if not rows:
        return {'n': 0, 'wr': None, 'pf': None, 'pnl': 0.0}
    wins = [r for r in rows if r['pnl'] > 0]
    gw = sum(r['pnl'] for r in wins)
    gl = -sum(r['pnl'] for r in rows if r['pnl'] <= 0)
    pf = round(gw / gl, 3) if gl > 0 else (float('inf') if gw > 0 else 0.0)
    return {'n': len(rows), 'wr': round(len(wins) / len(rows), 3), 'pf': pf,
            'pnl': round(sum(r['pnl'] for r in rows), 2)}


def weekly_pnl(rows):
    """{iso_week: {'net':, 'losses':}} keyed by EXIT (settlement) week."""
    wk = defaultdict(lambda: {'net': 0.0, 'losses': 0.0})
    for r in rows:
        w = iso_week(r['exit_date'])
        wk[w]['net'] += r['pnl']
        if r['pnl'] < 0:
            wk[w]['losses'] += r['pnl']
    return {k: {'net': round(v['net'], 2), 'losses': round(v['losses'], 2)}
            for k, v in wk.items()}


def extended_metrics(sim):
    rows = trade_rows(sim)
    h1 = [r for r in rows if r['entry_date'] <= H1_END]
    h2 = [r for r in rows if r['entry_date'] > H1_END]
    wk = weekly_pnl(rows)
    worst_loss_week = min(wk.items(), key=lambda kv: kv[1]['losses'],
                          default=(None, {'losses': 0.0}))
    worst_net_week = min(wk.items(), key=lambda kv: kv[1]['net'],
                         default=(None, {'net': 0.0}))
    # losing streak in exit order
    streak = best = 0
    for r in rows:
        streak = streak + 1 if r['pnl'] <= 0 else 0
        best = max(best, streak)
    # drawdown from end-of-day realized equity
    peak, maxdd, maxdd_pct = ACCOUNT, 0.0, 0.0
    for _, bal in sim.equity:
        peak = max(peak, bal)
        maxdd = max(maxdd, peak - bal)
        if peak > 0:
            maxdd_pct = max(maxdd_pct, (peak - bal) / peak)
    total = sum(r['pnl'] for r in rows)
    return {'full': seg_stats(rows), 'h1': seg_stats(h1), 'h2': seg_stats(h2),
            'total_pnl': round(total, 2),
            'pnl_per_month': round(total / MONTHS, 2),
            'end_balance': round(sim.balance, 2),
            'max_drawdown_dollars': round(maxdd, 2),
            'max_drawdown_pct_of_peak_equity': round(100 * maxdd_pct, 2),
            'worst_week_losses_only': {'week': worst_loss_week[0],
                                       'sum_losses': worst_loss_week[1]['losses']},
            'worst_week_net': {'week': worst_net_week[0],
                               'net': worst_net_week[1]['net']},
            'longest_losing_streak': best,
            'weekly_pnl': dict(sorted(wk.items()))}


# ------------------------------------------------------------------- running
def run_one(tag, strat_classes, max_concurrent, risk_pct, slip_scale,
            polygon, tradier, iv_daily, slip, closes, regime, allow_fresh=0):
    out_path = os.path.join(HERE, f'summary_scale_{tag}.json')
    if os.path.exists(out_path):
        print(f'skip {tag}: exists', flush=True)
        with open(out_path) as f:
            return json.load(f)
    prev_cap = sm.MAX_CONCURRENT
    sm.MAX_CONCURRENT = max_concurrent
    sm.MAX_FRESH_API_CALLS = allow_fresh
    try:
        print(f"\n=== {tag} risk={risk_pct} slip_scale={slip_scale} "
              f"maxconc={max_concurrent} ===", flush=True)
        sim = EquitySim(polygon, tradier, iv_daily, slip, closes, regime,
                        WINDOW_START, WINDOW_END,
                        risk_pct_equity=risk_pct, slip_scale=slip_scale)
        strats = [c() for c in strat_classes]
        base = sim.run(strats)
        ext = extended_metrics(sim)
        summary = {'tag': tag, 'account': ACCOUNT, 'risk_pct_equity': risk_pct,
                   'slip_scale': slip_scale, 'max_concurrent': max_concurrent,
                   'sizing': 'pct_of_current_equity_vs_max_loss_min1',
                   'window': [WINDOW_START, WINDOW_END], 'h1_end': H1_END,
                   **ext,
                   'by_exit_reason': base['by_exit_reason'],
                   'rejects': base['rejects'], 'coverage': base['coverage']}
        sim.dump_trades(os.path.join(HERE, f'trades_scale_{tag}.csv'))
        with open(out_path, 'w') as f:
            json.dump(summary, f, indent=2)
        print(f"--- {tag}: full n={ext['full']['n']} WR={ext['full']['wr']} "
              f"PF={ext['full']['pf']} P&L={ext['total_pnl']} "
              f"(${ext['pnl_per_month']}/mo) maxDD=${ext['max_drawdown_dollars']} "
              f"({ext['max_drawdown_pct_of_peak_equity']}%) "
              f"worstWkLoss={ext['worst_week_losses_only']['sum_losses']} "
              f"streak={ext['longest_losing_streak']} "
              f"fresh={base['coverage']['fresh_api_calls']}", flush=True)
        return summary
    finally:
        sm.MAX_CONCURRENT = prev_cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--phase', choices=['a', 'b'], default='a')
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--allow-fresh', type=int, default=0,
                    help='fresh-call budget (phase b sims should be ~0 after fetch)')
    args = ap.parse_args()

    iv_daily, slip = sm.load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))
    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)
    common = (polygon, tradier, iv_daily, slip, closes, regime)

    if args.phase == 'a':
        for pct, lbl in [(0.05, 'sz5'), (0.075, 'sz7p5'), (0.10, 'sz10'),
                         (0.15, 'sz15')]:
            for ss, suf in [(1.0, ''), (2.0, '_slip2x')]:
                tag = f'{lbl}{suf}'
                if args.only and tag not in args.only:
                    continue
                run_one(tag, [sl.SpyPutspreadWeeklyManaged], 3, pct, ss, *common)
    else:
        for tag, (classes, cap) in VARIANTS.items():
            for ss, suf in [(1.0, ''), (2.0, '_slip2x')]:
                full_tag = f'{tag}{suf}'
                if args.only and full_tag not in args.only:
                    continue
                run_one(full_tag, classes, cap, 0.05, ss, *common,
                        allow_fresh=args.allow_fresh)
    print('\nDONE', flush=True)


if __name__ == '__main__':
    main()
