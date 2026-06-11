#!/usr/bin/env python3
"""STEP 2/3 analysis over the shortlist trade logs (trades_sl_<tag>.csv).

Computes per tag (and per S4 IV-RV post-filtered variant): n, WR, PF, total
P&L, max realized DD, monthly P&L, H1 vs H2 split (H1 = entries <= 2025-08-28).

S4 = entry-time-only IV-RV filter applied as a POST-FILTER on the unfiltered
s1a / s2a logs (thresholds 0 / 2 / 4 vol points; primary measure = prior
session's VIX - trailing-21d realized vol from ivrv_series.csv). Valid because
those strategies hold at most one position at a time and the filter never
changes in-trade behavior, sizing, or the daily-halt path.

Output: shortlist_results.json + a markdown table on stdout.
"""
import bisect
import csv
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
H1_END = '2025-08-28'

IVRV_COL = 'vix_minus_rv'
S4_BASES = ['s1a_condor_weekly', 's1a_condor_weekly_slip2x',
            's2a_putspread_weekly', 's2a_putspread_weekly_slip2x']
S4_THRESHOLDS = [0.0, 2.0, 4.0]


def load_ivrv():
    by_date, dates = {}, []
    with open(os.path.join(HERE, 'ivrv_series.csv')) as f:
        for r in csv.DictReader(f):
            by_date[r['date']] = r
            dates.append(r['date'])
    return by_date, sorted(dates)


def ivrv_at_entry(by_date, dates, entry_date, col=IVRV_COL):
    i = bisect.bisect_left(dates, entry_date) - 1
    while i >= 0:
        v = by_date[dates[i]][col]
        if v != '':
            return float(v)
        i -= 1
    return None


def load_trades(tag):
    path = os.path.join(HERE, f'trades_sl_{tag}.csv')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r['pnl'] = float(r['pnl'])
    return rows


def stats(trades):
    if not trades:
        return {'n': 0, 'wr': None, 'pf': None, 'pnl': 0.0, 'maxdd': 0.0,
                'monthly': {}}
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    gw = sum(t['pnl'] for t in wins)
    gl = -sum(t['pnl'] for t in losses)
    eq, peak, maxdd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: (x['exit_date'], x['exit_time_et'])):
        eq += t['pnl']
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)
    monthly = defaultdict(float)
    for t in trades:
        monthly[t['entry_date'][:7]] += t['pnl']
    pf = (round(gw / gl, 3) if gl > 0
          else (float('inf') if gw > 0 else 0.0))
    return {'n': len(trades), 'wr': round(len(wins) / len(trades), 3),
            'pf': pf, 'pnl': round(sum(t['pnl'] for t in trades), 2),
            'avg_win': round(gw / len(wins), 2) if wins else None,
            'avg_loss': round(-gl / len(losses), 2) if losses else None,
            'maxdd': round(maxdd, 2),
            'monthly': {k: round(v, 2) for k, v in sorted(monthly.items())}}


def full_h1_h2(trades):
    h1 = [t for t in trades if t['entry_date'] <= H1_END]
    h2 = [t for t in trades if t['entry_date'] > H1_END]
    return {'full': stats(trades), 'h1': stats(h1), 'h2': stats(h2)}


def main():
    by_date, dates = load_ivrv()
    tags = sorted(set(
        f[len('trades_sl_'):-len('.csv')]
        for f in os.listdir(HERE)
        if f.startswith('trades_sl_') and f.endswith('.csv')
        and not f.startswith('trades_sl_smoke')))
    results = {}
    for tag in tags:
        trades = load_trades(tag)
        if trades is None:
            continue
        results[tag] = full_h1_h2(trades)
    # S4 post-filters
    for base in S4_BASES:
        trades = load_trades(base)
        if trades is None:
            continue
        for t in trades:
            t['ivrv'] = ivrv_at_entry(by_date, dates, t['entry_date'])
        for thr in S4_THRESHOLDS:
            sub = [t for t in trades if t['ivrv'] is not None and t['ivrv'] > thr]
            key = f's4[{base}]_ivrv>{thr:g}'
            results[key] = full_h1_h2(sub)
            results[key]['note'] = (f'post-filter of {base}; '
                                    f'{len(trades) - len(sub)} entries removed')
    with open(os.path.join(HERE, 'shortlist_results.json'), 'w') as f:
        json.dump(results, f, indent=2)

    def row(tag, r):
        def cell(s):
            if s['n'] == 0:
                return '0 / - / - / -'
            pf = 'inf' if s['pf'] == float('inf') else f"{s['pf']:.2f}"
            return f"{s['n']} / {s['wr']:.0%} / {pf} / {s['pnl']:+,.0f}"
        return (f"| {tag} | {cell(r['full'])} | {cell(r['h1'])} | "
                f"{cell(r['h2'])} | {r['full']['maxdd']:,.0f} |")

    print('| strategy/scenario | FULL n/WR/PF/P&L | H1 | H2 | maxDD |')
    print('|---|---|---|---|---|')
    for tag in results:
        print(row(tag, results[tag]))
    print('\nmonthly P&L (by entry month):')
    for tag in results:
        if 'slip2x' in tag or 'slip5pct' in tag:
            continue
        print(f"  {tag}: {results[tag]['full']['monthly']}")


if __name__ == '__main__':
    main()
