#!/usr/bin/env python3
"""Assemble results tables from batch_results.json + baseline log/trades.

Prints markdown tables for the final report:
  - per-run results (n, wr, avg win/loss, PF, pnl, maxDD)
  - monthly breakdown for key runs
  - by-regime breakdown
  - half-1 plateau matrix
  - half1 vs half2 consistency
  - ablation table
"""
import csv
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def row_for(tag, r):
    s = r['summary']
    pf = s['profit_factor']
    pf_s = f"{pf:.2f}" if pf != float('inf') else 'inf'
    return (f"| {tag} | {s['n_trades']} | {s['win_rate']*100:.0f}% "
            f"| {s['avg_win']:.0f} | {s['avg_loss']:.0f} | {pf_s} "
            f"| {s['total_pnl']:+,.0f} | {s['max_drawdown']:,.0f} |")


def monthly_table(results, tags):
    months = sorted({m for t in tags if t in results
                     for m in results[t]['summary']['monthly']})
    out = ['| month | ' + ' | '.join(tags) + ' |',
           '|---' * (len(tags) + 1) + '|']
    for m in months:
        cells = [f"{results[t]['summary']['monthly'].get(m, 0):+,.0f}"
                 if t in results else '-' for t in tags]
        out.append(f"| {m} | " + ' | '.join(cells) + ' |')
    return '\n'.join(out)


def plateau(results):
    print('\n### Half-1 plateau (total P&L $ / PF / n)')
    for cap in ('25', '30', '35'):
        print(f"\n**delta cap 0.{cap}** (rows=stop, cols=TP)\n")
        print('| stop \\ TP | 40% | 50% | 60% |')
        print('|---|---|---|---|')
        for stop in ('0.75', '1.0', '1.5'):
            cells = []
            for tp in ('40', '50', '60'):
                tag = f'h1_s{stop}_t{tp}_d{cap}'
                if tag in results:
                    s = results[tag]['summary']
                    pf = s['profit_factor']
                    pf_s = f"{pf:.2f}" if pf != float('inf') else 'inf'
                    cells.append(f"{s['total_pnl']:+,.0f} / {pf_s} / {s['n_trades']}")
                else:
                    cells.append('-')
            print(f"| {stop}x | " + ' | '.join(cells) + ' |')


def main():
    with open(os.path.join(HERE, 'batch_results.json')) as f:
        results = json.load(f)

    print('### Results table (proposed-rules runs)')
    print('| run | n | win% | avgW $ | avgL $ | PF | totalP&L $ | maxDD $ |')
    print('|---|---|---|---|---|---|---|---|')
    order = ['proposed_full', 'oos_half2', 'abl_no_regime', 'abl_no_iv', 'abl_no_stop']
    h1lock = 'h1_s1.0_t50_d30'
    if h1lock in results:
        order.insert(1, h1lock)
    for tag in order:
        if tag in results:
            print(row_for(tag, results[tag]))

    plateau(results)

    print('\n### Monthly P&L')
    print(monthly_table(results, ['proposed_full', 'abl_no_regime', 'abl_no_iv', 'abl_no_stop']))

    print('\n### Half-1 (locked) vs Half-2 (untouched)')
    print('| run | n | win% | avgW | avgL | PF | P&L | maxDD |')
    print('|---|---|---|---|---|---|---|---|')
    for tag in (h1lock, 'oos_half2'):
        if tag in results:
            print(row_for(tag, results[tag]))

    print('\n### By-regime (proposed_full)')
    if 'proposed_full' in results:
        br = results['proposed_full']['summary']['by_regime']
        print('| regime|side | n | P&L $ |')
        print('|---|---|---|')
        for k, (n, pnl) in sorted(br.items()):
            print(f'| {k} | {n} | {pnl:+,.0f} |')
        print('\nrejects:', json.dumps(results['proposed_full']['summary']['rejects']))


if __name__ == '__main__':
    main()
