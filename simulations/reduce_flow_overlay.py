#!/usr/bin/env python3
"""Droplet-side (READ-ONLY) reducer for the flow-overlay study.

Run:  ssh root@droplet "python3 - | gzip -c" < reduce_flow_overlay.py \
          > flow_overlay_reduced.csv.gz

Streams every whalestream_options daily JSON, dedupes by uuid (falling back
to id), and emits one small CSV row per unique alert:
    utc (YYYY-MM-DD HH:MM:SS), sym, cls (S stock / E etf / I index),
    cp (C/P), prem ($), bai (bid_ask_indicator), sweep (0/1)
Writes NOTHING to droplet disk; progress goes to stderr.
"""
import csv
import glob
import json
import sys

files = sorted(glob.glob(
    '/root/trading-bot/data/whalestream_options/options_alerts_*.json'))
w = csv.writer(sys.stdout)
w.writerow(['utc', 'sym', 'cls', 'cp', 'prem', 'bai', 'sweep'])
seen = set()
n_raw = n_out = 0
for fp in files:
    try:
        with open(fp) as f:
            alerts = json.load(f)
    except Exception as e:
        sys.stderr.write('SKIP %s: %s\n' % (fp, e))
        continue
    for a in alerts:
        n_raw += 1
        u = a.get('uuid') or a.get('id')
        if u is None or u in seen:
            continue
        seen.add(u)
        asset = a.get('asset') or {}
        sym = (a.get('symbol') or asset.get('ticker') or '').upper()
        d = (a.get('date') or '').replace('T', ' ')[:19]
        if not sym or not d:
            continue
        cls = 'I' if asset.get('is_index') else (
            'E' if asset.get('is_etf') else 'S')
        spec = (a.get('formatted_specific_type') or '').lower()
        w.writerow([d, sym, cls, 'C' if a.get('is_call') else 'P',
                    a.get('premium') or 0, a.get('bid_ask_indicator') or '',
                    1 if 'sweep' in spec else 0])
        n_out += 1
    del alerts
    sys.stderr.write('%s done (raw=%d uniq=%d)\n'
                     % (fp.split('/')[-1], n_raw, n_out))
sys.stderr.write('TOTAL raw=%d uniq=%d\n' % (n_raw, n_out))
