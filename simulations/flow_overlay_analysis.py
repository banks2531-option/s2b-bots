#!/usr/bin/env python3
"""Flow-overlay study: join pre-registered features to (1) the daily S2b
entry panel and (2) the real Monday S2b trade set; run the pre-registered
bucket tests and the H1-fit/H2-frozen veto + sizing production tests.

Outputs: flow_overlay_results.json + printed tables (paste into report).
Run AFTER run_panel.py. Gates per flow_overlay_prereg.md (verbatim).
"""
import csv
import json
import math
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
H1_END = '2025-08-28'
FEATURES = ['idx_put_share_am', 'idx_put_ask_prem_am', 'idx_put_sweep_cnt_am',
            'breadth_bear_am', 'dp_prem_am', 'idx_put_share_prior',
            'idx_put_ask_prem_prior', 'dvix', 'alert_cnt_am']
# dp_idx_sell_share_am excluded: blank on all 250 sessions (<5 indicator
# prints every morning) — documented absence per prereg.


def load_trades(path):
    out = []
    with open(os.path.join(HERE, path), newline='') as f:
        for r in csv.DictReader(f):
            out.append({'entry_date': r['entry_date'],
                        'exit_date': r['exit_date'],
                        'exit_reason': r['exit_reason'],
                        'pnl': float(r['pnl'])})
    out.sort(key=lambda x: (x['exit_date'], x['entry_date']))
    return out


def load_features():
    feats = {}
    with open(os.path.join(HERE, 'flow_features.csv'), newline='') as f:
        for r in csv.DictReader(f):
            feats[r['date']] = {k: (float(v) if v not in ('', None) else None)
                                for k, v in r.items() if k != 'date'}
    return feats


def pf(rows):
    gw = sum(r['pnl'] for r in rows if r['pnl'] > 0)
    gl = -sum(r['pnl'] for r in rows if r['pnl'] <= 0)
    return round(gw / gl, 3) if gl > 0 else (float('inf') if gw > 0 else 0.0)


def seg(rows):
    if not rows:
        return {'n': 0, 'mean': None, 'wr': None, 'pf': None, 'pnl': 0.0}
    wins = sum(1 for r in rows if r['pnl'] > 0)
    return {'n': len(rows), 'mean': round(st.mean(r['pnl'] for r in rows), 2),
            'wr': round(wins / len(rows), 3), 'pf': pf(rows),
            'pnl': round(sum(r['pnl'] for r in rows), 2)}


def halves(rows):
    return ([r for r in rows if r['entry_date'] <= H1_END],
            [r for r in rows if r['entry_date'] > H1_END])


def welch_t(a, b):
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = st.variance(a), st.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((st.mean(a) - st.mean(b)) / se, 2) if se > 0 else None


def quantile(vals, q):
    s = sorted(vals)
    idx = q * (len(s) - 1)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def maxdd(rows):
    eq = peak = dd = 0.0
    for r in rows:
        eq += r['pnl']
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


# -------------------------------------------------- panel bucket test
def panel_test(panel, feats, fname):
    valid = [dict(r, f=feats[r['entry_date']][fname]) for r in panel
             if r['entry_date'] in feats
             and feats[r['entry_date']][fname] is not None]
    if not valid:
        return {'feature': fname, 'error': 'no valid trades'}
    vals = [r['f'] for r in valid]
    q1, q2, q3 = (quantile(vals, .25), quantile(vals, .5), quantile(vals, .75))

    def bucket(r):
        return 0 if r['f'] <= q1 else 1 if r['f'] <= q2 else \
               2 if r['f'] <= q3 else 3
    for r in valid:
        r['b'] = bucket(r)
    sizes = [sum(1 for r in valid if r['b'] == b) for b in range(4)]
    mode = 'quartile'
    if min(sizes) < 15:                      # prereg fallback
        mode = 'median'
        for r in valid:
            r['b'] = 0 if r['f'] <= q2 else 3   # 0 = benign, 3 = hostile
    h1, h2 = halves(valid)
    buckets = {}
    use = range(4) if mode == 'quartile' else (0, 3)
    for b in use:
        buckets[f'Q{b+1}'] = {
            'full': seg([r for r in valid if r['b'] == b]),
            'h1': seg([r for r in h1 if r['b'] == b]),
            'h2': seg([r for r in h2 if r['b'] == b])}
    hostile = [r for r in valid if r['b'] == 3]
    kept = [r for r in valid if r['b'] != 3]

    # gates
    res = {'feature': fname, 'mode': mode,
           'cut_points': [round(q1, 4), round(q2, 4), round(q3, 4)],
           'n_valid': len(valid),
           'uncond': {'full': seg(valid), 'h1': seg(h1), 'h2': seg(h2)},
           'buckets': buckets,
           't_hostile_vs_rest': welch_t([r['pnl'] for r in hostile],
                                        [r['pnl'] for r in kept])}
    k1, k2 = halves(kept)
    ga = pf(k1) > pf(h1) and pf(k2) > pf(h2)
    res['gate_a_pf_improves_both_halves'] = {
        'h1_kept_pf': pf(k1), 'h1_uncond_pf': pf(h1),
        'h2_kept_pf': pf(k2), 'h2_uncond_pf': pf(h2), 'pass': ga}
    # gate b: drop the single best-saved trade (largest loss in hostile set)
    gb = False
    if hostile and ga:
        best_save = min(hostile, key=lambda r: r['pnl'])
        v2 = [r for r in valid if r is not best_save]
        k2v = [r for r in v2 if r['b'] != 3]
        gb = (pf(halves(k2v)[0]) > pf(halves(v2)[0])
              and pf(halves(k2v)[1]) > pf(halves(v2)[1]))
        res['gate_b_survives_best_save_removal'] = {
            'best_save': {'entry_date': best_save['entry_date'],
                          'pnl': best_save['pnl']}, 'pass': gb}
    else:
        res['gate_b_survives_best_save_removal'] = {'pass': False,
                                                    'note': 'gate_a failed or empty'}
    # gate c: hostile mean below panel mean in both halves
    hh1, hh2 = halves(hostile)
    gc = (bool(hh1) and bool(hh2)
          and st.mean(r['pnl'] for r in hh1) < st.mean(r['pnl'] for r in h1)
          and st.mean(r['pnl'] for r in hh2) < st.mean(r['pnl'] for r in h2))
    res['gate_c_same_direction_both_halves'] = gc
    res['PASS'] = bool(ga and gb and gc)
    return res


# -------------------------------------------------- production tests
def production_tests(mondays, feats, fname):
    # missing feature (archive gap) => NOT vetoable, 1.0x sizing: the
    # deployment rule is "no data -> trade normally". Baseline = ALL trades.
    valid = [dict(r, f=feats.get(r['entry_date'], {}).get(fname))
             for r in mondays]
    h1, h2 = halves(valid)
    base = {'full': seg(valid), 'h1': seg(h1), 'h2': seg(h2),
            'maxdd': maxdd(valid)}
    f1 = sorted(r['f'] for r in h1 if r['f'] is not None)
    thr = quantile(f1, .75)
    kept = [r for r in valid if r['f'] is None or r['f'] <= thr]
    kh1, kh2 = halves(kept)
    veto = {'threshold_h1_p75': round(thr, 4),
            'n_missing_feature': sum(1 for r in valid if r['f'] is None),
            'n_vetoed_h1': len(h1) - len(kh1), 'n_vetoed_h2': len(h2) - len(kh2),
            'full': seg(kept), 'h1': seg(kh1), 'h2': seg(kh2),
            'maxdd': maxdd(kept),
            'pf_delta_full': (round(pf(kept) - pf(valid), 3)
                              if math.isfinite(pf(kept)) and math.isfinite(pf(valid)) else None),
            'pnl_delta_full': round(sum(r['pnl'] for r in kept)
                                    - sum(r['pnl'] for r in valid), 2)}
    t1, t2 = quantile(f1, 1 / 3), quantile(f1, 2 / 3)
    sized = [dict(r, pnl=r['pnl'] * (1.0 if r['f'] is None else
                                     1.5 if r['f'] <= t1 else
                                     1.0 if r['f'] <= t2 else 0.5))
             for r in valid]
    sh1, sh2 = halves(sized)
    sizing = {'terciles_h1': [round(t1, 4), round(t2, 4)],
              'full': seg(sized), 'h1': seg(sh1), 'h2': seg(sh2),
              'maxdd': maxdd(sized),
              'pnl_delta_full': round(sum(r['pnl'] for r in sized)
                                      - sum(r['pnl'] for r in valid), 2)}
    return {'feature': fname, 'baseline': base, 'veto': veto, 'sizing': sizing}


def main():
    feats = load_features()
    results = {'panel': {}, 'production': {}}

    for ptag, pfile in [('baseline', 'trades_panel_daily.csv'),
                        ('slip2x', 'trades_panel_daily_slip2x.csv')]:
        if not os.path.exists(os.path.join(HERE, pfile)):
            continue
        panel = load_trades(pfile)
        results['panel'][ptag] = {'n': len(panel),
                                  'uncond': {'full': seg(panel),
                                             'h1': seg(halves(panel)[0]),
                                             'h2': seg(halves(panel)[1])},
                                  'features': {}}
        for fn in FEATURES:
            results['panel'][ptag]['features'][fn] = panel_test(panel, feats, fn)

    for ctag, mfile in [('baseline', 'trades_sl_s2b_putspread_managed.csv'),
                        ('slip2x', 'trades_sl_s2b_putspread_managed_slip2x.csv')]:
        mondays = load_trades(mfile)
        results['production'][ctag] = {'n': len(mondays), 'features': {}}
        for fn in FEATURES:
            results['production'][ctag]['features'][fn] = \
                production_tests(mondays, feats, fn)

    with open(os.path.join(HERE, 'flow_overlay_results.json'), 'w') as f:
        json.dump(results, f, indent=1, default=str)

    # ----- console tables
    for ptag, pres in results['panel'].items():
        u = pres['uncond']
        print(f"\n===== PANEL ({ptag}) n={pres['n']} "
              f"uncond PF full/h1/h2 = {u['full']['pf']}/{u['h1']['pf']}/{u['h2']['pf']} =====")
        for fn, r in pres['features'].items():
            if 'error' in r:
                print(f"{fn}: {r['error']}")
                continue
            ga = r['gate_a_pf_improves_both_halves']
            print(f"\n--- {fn} ({r['mode']}, n={r['n_valid']}) "
                  f"t(hostile-rest)={r['t_hostile_vs_rest']} PASS={r['PASS']}")
            for b, s in r['buckets'].items():
                print(f"  {b}: full n={s['full']['n']:3d} mean={s['full']['mean']:>8} "
                      f"wr={s['full']['wr']} pf={s['full']['pf']} | "
                      f"h1 n={s['h1']['n']:3d} mean={s['h1']['mean']:>8} pf={s['h1']['pf']} | "
                      f"h2 n={s['h2']['n']:3d} mean={s['h2']['mean']:>8} pf={s['h2']['pf']}")
            print(f"  gateA kept-PF h1 {ga['h1_kept_pf']} vs {ga['h1_uncond_pf']}, "
                  f"h2 {ga['h2_kept_pf']} vs {ga['h2_uncond_pf']} -> {ga['pass']}; "
                  f"gateB {r['gate_b_survives_best_save_removal']['pass']}; "
                  f"gateC {r['gate_c_same_direction_both_halves']}")
    for ctag, pres in results['production'].items():
        print(f"\n===== PRODUCTION Mondays ({ctag}) n={pres['n']} =====")
        for fn, r in pres['features'].items():
            b, v, s = r['baseline'], r['veto'], r['sizing']
            print(f"{fn}: base PF {b['full']['pf']} (h1 {b['h1']['pf']} h2 {b['h2']['pf']}) "
                  f"pnl {b['full']['pnl']} dd {b['maxdd']} || "
                  f"VETO({v['n_vetoed_h1']}+{v['n_vetoed_h2']}) PF {v['full']['pf']} "
                  f"(h1 {v['h1']['pf']} h2 {v['h2']['pf']}) pnl {v['full']['pnl']} "
                  f"dd {v['maxdd']} || SIZE PF {s['full']['pf']} "
                  f"(h1 {s['h1']['pf']} h2 {s['h2']['pf']}) pnl {s['full']['pnl']} dd {s['maxdd']}")


if __name__ == '__main__':
    main()
