#!/usr/bin/env python3
"""Build the pre-registered flow-overlay features (flow_overlay_prereg.md)
per trading session 2025-03-03 -> 2026-02-27. NO outcome data is touched here.

Inputs:
  flow_overlay_reduced.csv.gz   (droplet reduce: utc,sym,cls,cp,prem,bai,sweep)
  C:/Users/banks/trading-bot/data/whalestream_darkpool_merged.csv
  ivrv_series.csv               (vix closes)
Output:
  flow_features.csv             (date, F1..F10 by registered names)
"""
import csv
import gzip
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import underlying_bars as ub

ET = ZoneInfo('America/New_York')
WINDOW_START, WINDOW_END = '2025-03-03', '2026-02-27'
INDEX_SET = {'SPY', 'SPX', 'SPXW', 'QQQ'}
ASK = {'A', 'AA', 'TA'}
BID = {'B', 'BB', 'TB'}
DP_CSV = r'C:\Users\banks\trading-bot\data\whalestream_darkpool_merged.csv'
AM_START, AM_END = dtime(4, 0), dtime(10, 0)
FULL_END = dtime(20, 0)


def to_et(utc_str):
    # '2025-03-04 21:48:53' (UTC) -> ET datetime
    d = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S')
    return d.replace(tzinfo=timezone.utc).astimezone(ET)


def log10p(x):
    import math
    return round(math.log10(1.0 + max(0.0, x)), 4)


def main():
    sessions = ub.trading_sessions(WINDOW_START, WINDOW_END)
    sset = set(sessions)
    prior = {sessions[i]: sessions[i - 1] for i in range(1, len(sessions))}

    # ---------------- options flow accumulators
    Z = lambda: defaultdict(float)
    idx_am, idx_full = defaultdict(Z), defaultdict(Z)   # date -> agg
    breadth = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # date->tkr->[bull,bear]
    cnt_am = defaultdict(int)
    n = 0
    with gzip.open(os.path.join(HERE, 'flow_overlay_reduced.csv.gz'),
                   'rt', newline='') as f:
        for row in csv.DictReader(f):
            n += 1
            try:
                et = to_et(row['utc'])
            except ValueError:
                continue
            ds = et.strftime('%Y-%m-%d')
            t = et.time()
            am = AM_START <= t < AM_END
            full = AM_START <= t < FULL_END
            if not full:
                continue
            prem = float(row['prem'] or 0)
            sym, cp, bai = row['sym'], row['cp'], row['bai']
            if am:
                cnt_am[ds] += 1
            if sym in INDEX_SET:
                for agg, on in ((idx_am[ds], am), (idx_full[ds], full)):
                    if not on:
                        continue
                    agg['tot'] += prem
                    if cp == 'P':
                        agg['put'] += prem
                        if bai in ASK:
                            agg['put_ask'] += prem
                        if row['sweep'] == '1':
                            agg['put_sweep'] += 1
            elif am and row['cls'] == 'S':
                b = breadth[ds][sym]
                if (cp == 'C' and bai in ASK) or (cp == 'P' and bai in BID):
                    b[0] += prem
                elif (cp == 'P' and bai in ASK) or (cp == 'C' and bai in BID):
                    b[1] += prem
    print(f'flow rows processed: {n}', flush=True)

    # ---------------- dark pool accumulators
    dp_am = defaultdict(Z)
    ndp = 0
    with open(DP_CSV, newline='') as f:
        for row in csv.DictReader(f):
            ndp += 1
            try:
                et = to_et(row['date'][:19].replace('T', ' '))
            except ValueError:
                continue
            t = et.time()
            if not (AM_START <= t < AM_END):
                continue
            ds = et.strftime('%Y-%m-%d')
            prem = float(row['premium'] or 0)
            agg = dp_am[ds]
            agg['tot'] += prem
            if row['symbol'] in ('SPY', 'QQQ'):
                bai = row['bid_ask_indicator']
                if bai in BID:
                    agg['idx_bid'] += prem
                    agg['idx_n'] += 1
                elif bai in ASK:
                    agg['idx_ask'] += prem
                    agg['idx_n'] += 1
    print(f'darkpool rows processed: {ndp}', flush=True)

    # ---------------- VIX
    vix = {}
    with open(os.path.join(HERE, 'ivrv_series.csv'), newline='') as f:
        for row in csv.DictReader(f):
            if row['vix']:
                vix[row['date']] = float(row['vix'])
    vdates = sorted(vix)

    def dvix(ds):
        pri = [d for d in vdates if d < ds]
        if len(pri) < 2:
            return ''
        return round(vix[pri[-1]] - vix[pri[-2]], 2)

    # ---------------- assemble
    out_path = os.path.join(HERE, 'flow_features.csv')
    cols = ['date', 'idx_put_share_am', 'idx_put_ask_prem_am',
            'idx_put_sweep_cnt_am', 'breadth_bear_am', 'dp_prem_am',
            'dp_idx_sell_share_am', 'idx_put_share_prior',
            'idx_put_ask_prem_prior', 'dvix', 'alert_cnt_am']
    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for ds in sessions:
            a = idx_am.get(ds, {})
            f1 = round(a.get('put', 0) / a['tot'], 4) if a.get('tot') else ''
            f2 = log10p(a.get('put_ask', 0)) if a else ''
            f3 = int(a.get('put_sweep', 0)) if a else 0
            br = breadth.get(ds, {})
            dirs = [(b, s) for b, s in br.values() if b + s > 0]
            f4 = (round(sum(1 for b, s in dirs if s > b) / len(dirs), 4)
                  if len(dirs) >= 10 else '')
            d = dp_am.get(ds, {})
            f5 = log10p(d.get('tot', 0)) if d else 0.0
            side = d.get('idx_bid', 0) + d.get('idx_ask', 0)
            f6 = (round(d.get('idx_bid', 0) / side, 4)
                  if d.get('idx_n', 0) >= 5 and side > 0 else '')
            p = idx_full.get(prior.get(ds, ''), {})
            f7 = round(p.get('put', 0) / p['tot'], 4) if p.get('tot') else ''
            f8 = log10p(p.get('put_ask', 0)) if p else ''
            w.writerow([ds, f1, f2, f3, f4, f5, f6, f7, f8, dvix(ds),
                        cnt_am.get(ds, 0)])
    print(f'wrote {out_path} ({len(sessions)} sessions)', flush=True)


if __name__ == '__main__':
    main()
