#!/usr/bin/env python3
"""Bulk option-bar fetcher: Databento OPRA.PILLAR -> two-tier disk cache.

Replaces the throttled Polygon path for BULK history pulls (2026-06-11 plan).
Given a list of (OCC_symbol, ET_date) needs (JSON produced by
enumerate_misses.py), this:

  1. pads each need by +/-1 trading day,
  2. groups needs into weekly date-range buckets (one timeseries.get_range
     per bucket, all symbols of that bucket in one request; chunked at 1,500
     symbols, well under Databento's ~2,000/request limit),
  3. pulls schema=ohlcv-1m, stype_in=raw_symbol (OSI 21-char symbology,
     root left-padded to 6 chars: "SPY   250620P00590000"),
  4. aggregates 1-min -> 5-min ET-session bars aligned to wall-clock 5-min
     boundaries (t = floor(epoch_ms / 300000) * 300000, matching Polygon),
  5. writes one overlay key per (contract, ET session date) in EXACTLY the
     format the engine/harness reads:
         polygon:bars_{OCC}_{date}_{date}_5_minute
            -> [{t(epoch-ms), o, h, l, c, v}, ...]
     Sessions inside a fetched range with no prints are stored as [] so the
     engine never falls through to a live Polygon call.

Existing keys (E:\\ archive or overlay) are NEVER overwritten - completed
runs keep their original Polygon-sourced series.

Cost/usage is logged to databento_pull.log (symbols, requests, bytes, and
metadata.get_cost per bucket when available). The API key is read from
E:\\BanksBackup\\trading-bot\\.env at runtime and never printed.

Usage:
    python databento_bars.py --verify            # round-trip check, 1 contract-day
    python databento_bars.py --needs shortlist_misses.json
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

ET = ZoneInfo('America/New_York')
ENV_PATH = r'E:\BanksBackup\trading-bot\.env'
LOG_PATH = os.path.join(HERE, 'databento_pull.log')
DATASET = 'OPRA.PILLAR'
SCHEMA = 'ohlcv-1m'
MAX_SYMBOLS_PER_REQ = 1500
OCC_RE = re.compile(r'^([A-Z]+)(\d{6})([CP])(\d{8})$')

import engine_v345 as eng                      # holiday calendar
from disk_cache import get_disk_cache


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} | {msg}"
    print(line, flush=True)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def load_api_key():
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith('DATABENTO_API_KEY'):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise RuntimeError('DATABENTO_API_KEY not found in env file')


def occ_to_osi(occ):
    """Unpadded OCC (SPY250620P00590000) -> 21-char OSI (root padded to 6)."""
    m = OCC_RE.match(occ)
    if not m:
        raise ValueError(f'bad OCC symbol: {occ}')
    root, ymd, cp, strike = m.groups()
    return f"{root:<6}{ymd}{cp}{strike}"


def trading_sessions(start, end):
    cur = datetime.strptime(start, '%Y-%m-%d')
    stop = datetime.strptime(end, '%Y-%m-%d')
    out = []
    while cur <= stop:
        ds = cur.strftime('%Y-%m-%d')
        if cur.weekday() < 5 and ds not in eng.US_MARKET_HOLIDAYS:
            out.append(ds)
        cur += timedelta(days=1)
    return out


def adjacent_sessions(ds):
    """(previous, next) trading session around ds."""
    d = datetime.strptime(ds, '%Y-%m-%d')
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5 or prev.strftime('%Y-%m-%d') in eng.US_MARKET_HOLIDAYS:
        prev -= timedelta(days=1)
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5 or nxt.strftime('%Y-%m-%d') in eng.US_MARKET_HOLIDAYS:
        nxt += timedelta(days=1)
    return prev.strftime('%Y-%m-%d'), nxt.strftime('%Y-%m-%d')


def key_for(occ, ds):
    return f"polygon:bars_{occ}_{ds}_{ds}_5_minute"


def key_exists(dc, key):
    if dc.src.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone():
        return True
    if dc.conn.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone():
        return True
    return False


def aggregate_1m_to_5m(rows):
    """rows = [(epoch_ms, o, h, l, c, v)] (1-min, any order) -> 5-min bars."""
    buckets = defaultdict(list)
    for r in rows:
        buckets[(r[0] // 300_000) * 300_000].append(r)
    out = []
    for t5 in sorted(buckets):
        rs = sorted(buckets[t5], key=lambda r: r[0])
        out.append({'t': t5,
                    'o': rs[0][1],
                    'h': max(r[2] for r in rs),
                    'l': min(r[3] for r in rs),
                    'c': rs[-1][4],
                    'v': int(sum(r[5] for r in rs))})
    return out


def et_day_bounds_utc(start_ds, end_ds):
    """[ET start_ds 00:00, ET end_ds+1d 00:00) as UTC datetimes."""
    s = datetime.strptime(start_ds, '%Y-%m-%d').replace(tzinfo=ET)
    e = (datetime.strptime(end_ds, '%Y-%m-%d') + timedelta(days=1)).replace(tzinfo=ET)
    return s.astimezone(timezone.utc), e.astimezone(timezone.utc)


def fetch_bucket(client, symbols_osi, start_ds, end_ds, retries=4):
    """One get_range for a symbol set over [start_ds, end_ds] ET sessions.
    Returns ({osi_symbol: {et_date: [(ms,o,h,l,c,v),...]}}, nbytes).
    Retries transient gateway errors (e.g. 504) with linear backoff."""
    import time as _time
    from databento.common.error import BentoServerError
    s_utc, e_utc = et_day_bounds_utc(start_ds, end_ds)
    store = None
    for attempt in range(retries + 1):
        try:
            store = client.timeseries.get_range(
                dataset=DATASET, schema=SCHEMA, stype_in='raw_symbol',
                symbols=symbols_osi, start=s_utc, end=e_utc)
            break
        except BentoServerError as e:
            if attempt == retries:
                raise
            wait = 10 * (attempt + 1)
            log(f"  transient server error; retry {attempt + 1}/{retries} in {wait}s")
            _time.sleep(wait)
    nbytes = getattr(store, 'nbytes', None)
    df = store.to_df()                 # prices auto-scaled, symbol mapped
    per = defaultdict(lambda: defaultdict(list))
    if len(df):
        idx_ns = df.index.view('int64')         # ts_event ns UTC (bar open)
        syms = df['symbol'].to_numpy()
        o = df['open'].to_numpy(); h = df['high'].to_numpy()
        l = df['low'].to_numpy(); c = df['close'].to_numpy()
        v = df['volume'].to_numpy()
        for i in range(len(df)):
            ms = int(idx_ns[i] // 1_000_000)
            et_date = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) \
                .astimezone(ET).strftime('%Y-%m-%d')
            per[syms[i]][et_date].append(
                (ms, float(o[i]), float(h[i]), float(l[i]), float(c[i]), int(v[i])))
    if nbytes is None:
        nbytes = len(df) * 56          # ohnv-1m record size approximation
    return per, nbytes


def bucket_cost(client, symbols_osi, start_ds, end_ds):
    try:
        s_utc, e_utc = et_day_bounds_utc(start_ds, end_ds)
        return client.metadata.get_cost(
            dataset=DATASET, schema=SCHEMA, stype_in='raw_symbol',
            symbols=symbols_osi, start=s_utc, end=e_utc)
    except Exception as e:
        log(f"  (get_cost failed: {type(e).__name__}: {e})")
        return None


def plan_buckets(needs):
    """needs = [(occ, ds), ...] -> buckets keyed by ISO-week of the symbol's
    padded range start; each bucket = (start_ds, end_ds, {occ: set(dates)})."""
    by_occ = defaultdict(set)
    for occ, ds in needs:
        by_occ[occ].add(ds)
    sym_ranges = {}
    for occ, dates in by_occ.items():
        lo, hi = min(dates), max(dates)
        p_lo, _ = adjacent_sessions(lo)
        _, p_hi = adjacent_sessions(hi)
        sym_ranges[occ] = (p_lo, p_hi)
    buckets = defaultdict(lambda: [None, None, {}])
    for occ, (lo, hi) in sym_ranges.items():
        wk = datetime.strptime(lo, '%Y-%m-%d').strftime('%G-W%V')
        b = buckets[wk]
        b[0] = lo if b[0] is None else min(b[0], lo)
        b[1] = hi if b[1] is None else max(b[1], hi)
        b[2][occ] = by_occ[occ]
    return dict(sorted(buckets.items()))


def run_fetch(needs_path):
    import databento as db
    client = db.Historical(load_api_key())
    dc = get_disk_cache()
    with open(needs_path) as f:
        needs = [tuple(x) for x in json.load(f)]
    # drop needs already cached (another process may have filled some)
    needs = [(o, d) for o, d in needs if not key_exists(dc, key_for(o, d))]
    buckets = plan_buckets(needs)
    n_syms = len({o for o, _ in needs})
    log(f"FETCH start: {len(needs)} contract-days, {n_syms} distinct contracts, "
        f"{len(buckets)} weekly buckets, overlay={dc.db_path}")
    tot_req = tot_bytes = tot_keys = tot_empty = 0
    tot_cost = 0.0
    cost_known = True
    for wk, (lo, hi, occ_dates) in buckets.items():
        occs = sorted(occ_dates)
        osi_map = {occ_to_osi(o): o for o in occs}
        osis = sorted(osi_map)
        sessions = trading_sessions(lo, hi)
        for i in range(0, len(osis), MAX_SYMBOLS_PER_REQ):
            chunk = osis[i:i + MAX_SYMBOLS_PER_REQ]
            cost = bucket_cost(client, chunk, lo, hi)
            if cost is None:
                cost_known = False
            else:
                tot_cost += cost
            per, nbytes = fetch_bucket(client, chunk, lo, hi)
            tot_req += 1
            tot_bytes += nbytes or 0
            wrote = empt = 0
            for osi in chunk:
                occ = osi_map[osi]
                day_map = per.get(osi, {})
                for ds in sessions:
                    k = key_for(occ, ds)
                    if key_exists(dc, k):
                        continue
                    bars = aggregate_1m_to_5m(day_map.get(ds, []))
                    dc.put(k, bars)
                    wrote += 1
                    if not bars:
                        empt += 1
            tot_keys += wrote
            tot_empty += empt
            log(f"{wk} [{lo}..{hi}] syms={len(chunk)} rows->keys={wrote} "
                f"(empty {empt}) bytes={nbytes} "
                f"cost={'%.4f' % cost if cost is not None else 'n/a'}")
    log(f"FETCH done: requests={tot_req} symbols={n_syms} bytes={tot_bytes:,} "
        f"keys_written={tot_keys} (empty {tot_empty}) "
        f"cost_usd={'%.4f' % tot_cost if cost_known else f'>={tot_cost:.4f} (partial)'}")


def run_verify():
    """Round-trip check: fetch ONE contract-day that already exists in the E:\\
    archive and compare structure/closes WITHOUT writing anything."""
    import databento as db
    import sqlite3
    client = db.Historical(load_api_key())
    dc = get_disk_cache()
    # archive sample seen in inspection: multi-day key; use its first day
    occ, ds = 'SPY250321C00580000', '2025-03-18'
    row = dc.src.execute(
        "SELECT value FROM cache WHERE key=?",
        ("polygon:bars_SPY250321C00580000_2025-03-18_2025-03-21_5_minute",)).fetchone()
    ref_all = json.loads(row[0])
    s_utc, e_utc = et_day_bounds_utc(ds, ds)
    s_ms, e_ms = int(s_utc.timestamp() * 1000), int(e_utc.timestamp() * 1000)
    ref = [b for b in ref_all if s_ms <= b['t'] < e_ms]
    per, nbytes = fetch_bucket(client, [occ_to_osi(occ)], ds, ds)
    got = aggregate_1m_to_5m(per.get(occ_to_osi(occ), {}).get(ds, []))
    log(f"VERIFY {occ} {ds}: polygon_bars={len(ref)} databento_bars={len(got)} "
        f"bytes={nbytes}")
    ref_by_t = {b['t']: b for b in ref}
    got_by_t = {b['t']: b for b in got}
    common = sorted(set(ref_by_t) & set(got_by_t))
    log(f"  common timestamps: {len(common)} "
        f"(polygon-only {len(ref_by_t) - len(common)}, "
        f"databento-only {len(got_by_t) - len(common)})")
    if common:
        diffs = [abs(ref_by_t[t]['c'] - got_by_t[t]['c']) for t in common]
        exact = sum(1 for d in diffs if d < 1e-9)
        log(f"  close match: exact {exact}/{len(common)}, max |diff| = {max(diffs):.4f}")
        for t in common[:3]:
            log(f"  sample t={t}: poly c={ref_by_t[t]['c']} dbn c={got_by_t[t]['c']} "
            f"poly v={ref_by_t[t]['v']} dbn v={got_by_t[t]['v']}")
    keys_ok = got and all(set(b) == {'t', 'o', 'h', 'l', 'c', 'v'} for b in got)
    t_aligned = got and all(b['t'] % 300_000 == 0 for b in got)
    log(f"  format: keys_ok={bool(keys_ok)} t_5min_aligned={bool(t_aligned)} "
        f"t_is_epoch_ms={bool(got and got[0]['t'] > 1e12)}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--needs', help='JSON list of [occ, et_date] pairs')
    ap.add_argument('--verify', action='store_true')
    args = ap.parse_args()
    if args.verify:
        run_verify()
    elif args.needs:
        run_fetch(args.needs)
    else:
        ap.print_help()
