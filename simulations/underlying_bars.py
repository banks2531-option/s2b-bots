#!/usr/bin/env python3
"""Fetch + cache 5-min UNDERLYING (stock) bars from Polygon/Massive.

Cache layout: one key per (symbol, ET session date) in the same two-tier
disk cache used for option bars (E:\\ archive read-only + local overlay;
set OVERLAY_DB=api_cache_overlay_batch.db before launching to reuse the
warmed overlay — disk_cache.py reads the env var at import time):

    polygon:ubars_{SYM}_{date}_5_minute  ->  [{t,o,h,l,c,v}, ...]

`t` is the raw Polygon unix-ms UTC bar-start timestamp. Conversion to ET is
done with zoneinfo (real DST handling — NOT the engine's fixed UTC-5 shortcut,
which is wrong by an hour Mar-Nov). get_bars() attaches 'date_et'/'time_et'
convenience fields on the way out; they are not stored.

Prefetch strategy: one ranged aggregates request per (symbol, calendar month)
(~4k bars incl. extended hours, single page), then split into per-day keys.
That is 24 API calls for SPY+QQQ x 12 months instead of ~500 per-day calls.
Sessions with no bars are stored as [] so later lookups never re-hit the API.

Usage:
    python underlying_bars.py            # prefetch SPY+QQQ 2025-03-03..2026-02-27
    from underlying_bars import get_bars # get_bars('SPY', '2025-06-02')
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v345 as eng          # POLYGON key/base URL, holiday calendar
from disk_cache import get_disk_cache

ET = ZoneInfo('America/New_York')
MIN_INTERVAL = 0.30                # pacing per task constraints
WINDOW_START = '2025-03-03'
WINDOW_END = '2026-02-27'
DEFAULT_SYMBOLS = ['SPY', 'QQQ']
LOG_PATH = os.path.join(HERE, 'prefetch_underlying.log')

_mem = {}
_last_call = [0.0]
api_calls = [0]                    # module-level fresh-call counter


def _log(msg, log_file=None):
    line = f"{datetime.now().strftime('%H:%M:%S')} | {msg}"
    print(line, flush=True)
    if log_file:
        log_file.write(line + '\n')
        log_file.flush()


def _key(symbol, date_str):
    return f"polygon:ubars_{symbol}_{date_str}_5_minute"


def ts_to_et(unix_ms):
    """Unix ms -> aware ET datetime (correct across DST)."""
    return datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc).astimezone(ET)


def trading_sessions(start, end):
    """Weekday, non-holiday ET dates in [start, end] per the engine calendar."""
    cur = datetime.strptime(start, '%Y-%m-%d')
    stop = datetime.strptime(end, '%Y-%m-%d')
    out = []
    while cur <= stop:
        ds = cur.strftime('%Y-%m-%d')
        if cur.weekday() < 5 and ds not in eng.US_MARKET_HOLIDAYS:
            out.append(ds)
        cur += timedelta(days=1)
    return out


def _rate_limit():
    elapsed = time.time() - _last_call[0]
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_call[0] = time.time()


def _request(url):
    _rate_limit()
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(f"{url}{sep}apiKey={eng.POLYGON_API_KEY}")
    req.add_header('Accept', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            api_calls[0] += 1
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(12)
            return _request(url)
        raise


def _fetch_range(symbol, start, end):
    """Raw ranged 5-min stock aggregates (paginated, usually one page)."""
    url = (f"{eng.POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}"
           f"/range/5/minute/{start}/{end}?adjusted=true&sort=asc&limit=50000")
    bars = []
    while url:
        data = _request(url)
        if not data:
            break
        results = data.get('results', [])
        bars.extend(results)
        url = data.get('next_url') if results else None
    return [{'t': b['t'], 'o': b['o'], 'h': b['h'], 'l': b['l'],
             'c': b['c'], 'v': b.get('v', 0)} for b in bars]


def _split_and_store(dc, symbol, bars, sessions):
    """Group ranged bars by ET date and store one key per session date."""
    by_date = {}
    for b in bars:
        by_date.setdefault(ts_to_et(b['t']).strftime('%Y-%m-%d'), []).append(b)
    stored = 0
    for ds in sessions:
        dc.put(_key(symbol, ds), by_date.get(ds, []))
        stored += 1
    return stored, sum(len(v) for d, v in by_date.items() if d in sessions)


def get_bars(symbol, date_str):
    """5-min underlying bars for one ET session date.

    Returns list of {t,o,h,l,c,v,date_et,time_et} (incl. pre/post-market bars;
    callers filter to RTH 09:30-16:00 via time_et). Cache miss falls back to a
    single-day API pull (paced) and stores the result in the local overlay.
    """
    mk = (symbol, date_str)
    if mk in _mem:
        return _mem[mk]
    dc = get_disk_cache()
    bars = dc.get(_key(symbol, date_str))
    if bars is None:
        bars = _fetch_range(symbol, date_str, date_str)
        # keep only bars that actually fall on this ET date
        bars = [b for b in bars if ts_to_et(b['t']).strftime('%Y-%m-%d') == date_str]
        dc.put(_key(symbol, date_str), bars)
    out = []
    for b in bars:
        et = ts_to_et(b['t'])
        bb = dict(b)
        bb['date_et'] = et.strftime('%Y-%m-%d')
        bb['time_et'] = et.strftime('%H:%M')
        out.append(bb)
    _mem[mk] = out
    return out


def _month_chunks(start, end):
    """[(month_label, first_session, last_session), ...] within the window."""
    sessions = trading_sessions(start, end)
    by_month = {}
    for ds in sessions:
        by_month.setdefault(ds[:7], []).append(ds)
    return [(m, v[0], v[-1], v) for m, v in sorted(by_month.items())]


def prefetch(symbols=DEFAULT_SYMBOLS, start=WINDOW_START, end=WINDOW_END):
    dc = get_disk_cache()
    chunks = _month_chunks(start, end)
    n_sessions = sum(len(c[3]) for c in chunks)
    with open(LOG_PATH, 'a') as lf:
        _log(f"prefetch start: {symbols} {start}..{end} "
             f"({n_sessions} sessions, {len(chunks)} months, overlay={dc.db_path})", lf)
        for symbol in symbols:
            for month, first, last, sessions in chunks:
                missing = [ds for ds in sessions if dc.get(_key(symbol, ds)) is None]
                if not missing:
                    _log(f"{symbol} {month}: all {len(sessions)} sessions cached, skip", lf)
                    continue
                bars = _fetch_range(symbol, first, last)
                stored, kept = _split_and_store(dc, symbol, bars, sessions)
                _log(f"{symbol} {month}: fetched {len(bars)} bars -> stored "
                     f"{stored} session keys ({kept} RTH+ext bars), "
                     f"api_calls={api_calls[0]}", lf)
        _log(f"prefetch done: {api_calls[0]} fresh API calls | cache {dc.stats()}", lf)


if __name__ == '__main__':
    syms = sys.argv[1:] or DEFAULT_SYMBOLS
    prefetch(syms)
