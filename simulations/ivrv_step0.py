#!/usr/bin/env python3
"""STEP 0 — measure the actual IV-RV premium in the backtest window (cache-only).

Daily series, one row per session in 2025-03-03 .. 2026-02-27:
  spy_close        SPY daily close (underlying_daily_closes.csv)
  rv21             trailing 21-session realized vol of SPY, annualized vol pts
                   (std of log daily returns * sqrt(252) * 100; min 15 returns)
  vix              cached VIX close (archive disk-cache keys vix_close_{date})
  iv_alert_spy     per-day median whalestream alert IV for SPY (vol points),
                   from mech_iv_slip_cache.json (sim_proposed.build_iv_daily)
  vix_minus_rv     vix - rv21          (the primary IV-RV premium measure)
  ivalert_minus_rv iv_alert_spy - rv21 (biased-high alt measure; alerts cluster
                                        in OTM/short-dated contracts)

All values are AS-OF that session's close. Any entry-time filter must use the
PRIOR session's row (point-in-time safe) — ivrv_filter_value() does that.

Outputs ivrv_series.csv + monthly and H1/H2 stats on stdout.
Zero API calls: VIX from archive cache, closes/IV from local files.
"""
import csv
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault('OVERLAY_DB', 'api_cache_overlay_batch.db')

WINDOW_START = '2025-03-03'
WINDOW_END = '2026-02-27'
H1_END = '2025-08-28'
OUT_CSV = os.path.join(HERE, 'ivrv_series.csv')


def load_spy_closes():
    closes = {}
    with open(os.path.join(HERE, 'underlying_daily_closes.csv')) as f:
        r = csv.reader(f)
        header = next(r)
        spy_i = header.index('SPY')
        for row in r:
            if row[spy_i]:
                closes[row[0][:10]] = float(row[spy_i])
    return closes


def load_vix():
    from disk_cache import get_disk_cache
    dc = get_disk_cache()
    rows = dc.src.execute(
        "SELECT key, value FROM cache WHERE key LIKE 'vix_close_%'").fetchall()
    return {k.replace('vix_close_', ''): json.loads(v) for k, v in rows}


def load_iv_alert():
    with open(os.path.join(HERE, 'mech_iv_slip_cache.json')) as f:
        return json.load(f)['iv_daily'].get('SPY', {})


def build_series():
    closes = load_spy_closes()
    vix = load_vix()
    iv_alert = load_iv_alert()
    dates = sorted(closes)
    rows = []
    for i, d in enumerate(dates):
        if not (WINDOW_START <= d <= WINDOW_END):
            continue
        # trailing 21-session realized vol through TODAY's close
        rets = []
        j = i
        while j >= 1 and len(rets) < 21:
            rets.append(math.log(closes[dates[j]] / closes[dates[j - 1]]))
            j -= 1
        rv = (statistics.stdev(rets) * math.sqrt(252) * 100.0
              if len(rets) >= 15 else None)
        v = vix.get(d)
        ia = iv_alert.get(d)
        ia_pts = ia * 100.0 if ia else None
        rows.append({
            'date': d, 'spy_close': round(closes[d], 2),
            'rv21': round(rv, 2) if rv is not None else '',
            'vix': v if v is not None else '',
            'iv_alert_spy': round(ia_pts, 2) if ia_pts is not None else '',
            'vix_minus_rv': round(v - rv, 2) if (v is not None and rv is not None) else '',
            'ivalert_minus_rv': round(ia_pts - rv, 2) if (ia_pts is not None and rv is not None) else '',
        })
    return rows


def ivrv_filter_value(series_by_date, sorted_dates, entry_date, col='vix_minus_rv'):
    """Point-in-time IV-RV spread available on entry MORNING = most recent
    session strictly before entry_date with a non-empty value."""
    import bisect
    i = bisect.bisect_left(sorted_dates, entry_date) - 1
    while i >= 0:
        v = series_by_date[sorted_dates[i]][col]
        if v != '':
            return float(v)
        i -= 1
    return None


def stats(vals):
    vals = [v for v in vals if v != '']
    if not vals:
        return None
    vals = [float(v) for v in vals]
    return (len(vals), statistics.mean(vals), statistics.median(vals))


def main():
    rows = build_series()
    with open(OUT_CSV, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f'wrote {OUT_CSV} ({len(rows)} sessions)')

    by_month = {}
    for r in rows:
        by_month.setdefault(r['date'][:7], []).append(r)
    print(f"\n{'month':8} {'n':>3} | {'VIX-RV mean':>11} {'med':>6} | "
          f"{'alertIV-RV mean':>15} {'med':>6} | {'VIX med':>7} {'RV med':>6}")
    for m, rs in sorted(by_month.items()):
        s1 = stats([r['vix_minus_rv'] for r in rs])
        s2 = stats([r['ivalert_minus_rv'] for r in rs])
        sv = stats([r['vix'] for r in rs])
        sr = stats([r['rv21'] for r in rs])
        print(f"{m:8} {len(rs):>3} | {s1[1]:>11.2f} {s1[2]:>6.2f} | "
              f"{s2[1]:>15.2f} {s2[2]:>6.2f} | {sv[2]:>7.2f} {sr[2]:>6.2f}"
              if s1 and s2 else f"{m:8} {len(rs):>3} | insufficient data")

    for label, lo, hi in [('FULL', WINDOW_START, WINDOW_END),
                          ('H1', WINDOW_START, H1_END),
                          ('H2', '2025-08-29', WINDOW_END)]:
        sel = [r for r in rows if lo <= r['date'] <= hi]
        s1 = stats([r['vix_minus_rv'] for r in sel])
        s2 = stats([r['ivalert_minus_rv'] for r in sel])
        pos = [float(r['vix_minus_rv']) for r in sel if r['vix_minus_rv'] != '']
        frac2 = sum(1 for v in pos if v > 2) / len(pos) if pos else 0
        frac0 = sum(1 for v in pos if v > 0) / len(pos) if pos else 0
        frac4 = sum(1 for v in pos if v > 4) / len(pos) if pos else 0
        print(f"\n{label}: VIX-RV  n={s1[0]} mean={s1[1]:+.2f} median={s1[2]:+.2f} "
              f"| %days>0: {frac0:.0%}  >2: {frac2:.0%}  >4: {frac4:.0%}")
        print(f"{'':6}alertIV-RV n={s2[0]} mean={s2[1]:+.2f} median={s2[2]:+.2f}")


if __name__ == '__main__':
    main()
