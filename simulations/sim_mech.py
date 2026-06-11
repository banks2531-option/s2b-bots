#!/usr/bin/env python3
"""Mechanical-strategy options simulator (entry origination DECOUPLED from flow).

Reuses engine_v345 pricing (Polygon 5-min option bars via the two-tier disk
cache) and the portfolio/exit machinery patterns of sim_proposed.py, but
entries come from a Strategy interface driven by 5-min UNDERLYING bars
(underlying_bars.py) instead of whalestream flow alerts:

    class MechStrategy:
        symbols = ['SPY']                     # underlyings whose bars drive on_bar
        def on_session_start(self, date, ctx): ...
        def on_bar(self, dt, bars_so_far, ctx) -> list[SpreadOrder]

SpreadOrder specifies symbol, structure (bull_put / bear_call / iron_condor /
long_call / long_put / calendar), short_delta target OR strike_offset, width,
DTE target, and per-order exit config (TP %, stop multiple, time-exit DTE,
EOD flatten, max hold days).

Conventions / proxy choices (documented per task):
  * Clock = completed 5-min RTH underlying bars in REAL ET (zoneinfo, correct
    across DST — unlike the engine's fixed UTC-5 ts_to_time, which is 1h off
    Mar–Nov; all time logic here uses unix-ms timestamps + zoneinfo).
  * on_bar(dt) fires at bar COMPLETION time; spot = last completed bar close;
    fills use the close of the first option bar starting at/after dt
    (same fill convention as sim_proposed: bar close ± half-spread per leg).
  * Slippage: empirical per-symbol NBBO half-spread per leg from
    sim_proposed.build_slippage_model (whalestream NBBO medians, floor $0.05),
    cached in mech_iv_slip_cache.json to avoid re-parsing the 1.27M-row CSV.
  * Delta: sim_proposed.bs_delta with sigma = per-symbol daily median alert IV
    (sim_proposed.build_iv_daily), most recent session STRICTLY BEFORE entry
    (point-in-time safe). Alert-median IV skews high vs ATM IV (OTM/short-dated
    alert mix), so realized short deltas may sit slightly further OTM than the
    target. For 0DTE, dte = fraction of day remaining to 16:00 ET (floor 0.01d).
  * Strikes: Tradier strikes cache first; ~99% of cached entries are EMPTY
    (poisoned [] from the legacy engine), and Tradier cannot serve historical
    expirations, so a synthetic grid is the workhorse: $1 grid for SPY/QQQ/IWM
    (their true listed increment near the money), price-scaled otherwise.
    No live Tradier calls are made for past expirations.
  * Option bars fetched PER DAY (polygon:bars_{OCC}_{d}_{d}_5_minute) so keys
    align with the archive's Databento-format daily keys and the overlay is
    maximally reusable. Coverage per (contract, day) is classified as
    archive / overlay / api_fresh / api_empty.
  * Commissions $0.65 per leg per contract round-trip (= sim_proposed's $1.30
    per 2-leg spread). Intrinsic expiration settlement carries no exit slip.
  * calendar structure is implemented (short near / long far, same strike,
    debit-style) but is approximate (max loss = debit; aligned bars only exist
    until near expiry) and is NOT exercised by the reference strategies.

Portfolio (same as sim_proposed): $10k, 5% risk/trade sizing vs max loss,
max 3 concurrent, 2% daily-loss halt on new entries.

Smoke: --strategy both --start 2025-06-01 --end 2025-06-30
"""
import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v345 as eng
import sim_proposed as sp
import underlying_bars as ub

ET = ZoneInfo('America/New_York')

ACCOUNT_START = 10_000.0
RISK_PCT = 0.05                  # of starting account -> $500/trade
MAX_CONCURRENT = 3
DAILY_LOSS_HALT = 0.02
COMMISSION_PER_LEG_RT = 0.65     # per contract per leg, round trip
SLIP_FLOOR = 0.05
MAX_FRESH_API_CALLS = 2500       # hard budget guard for this task
ENTRY_FILL_WINDOW_MIN = 30       # reject if no option bar within 30 min of signal
EOD_FLATTEN_ET = dtime(15, 50)
IV_SLIP_CACHE = os.path.join(HERE, 'mech_iv_slip_cache.json')

ETF_DOLLAR_GRID = {'SPY', 'QQQ', 'IWM'}


class BudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------- ET helpers
def ts_et(unix_ms):
    return datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc).astimezone(ET)


def et_ms(date_str, hh, mm):
    d = datetime.strptime(date_str, '%Y-%m-%d')
    return int(d.replace(hour=hh, minute=mm, tzinfo=ET).timestamp() * 1000)


def rth(bars):
    out = []
    for b in bars:
        t = ts_et(b['t']).time()
        if dtime(9, 30) <= t < dtime(16, 0):
            out.append(b)
    return out


# ------------------------------------------------------------- IV/slip cache
def load_iv_slip():
    """build_iv_daily + build_slippage_model from the flow CSV, JSON-cached."""
    if os.path.exists(IV_SLIP_CACHE):
        with open(IV_SLIP_CACHE) as f:
            d = json.load(f)
        return d['iv_daily'], d['slip']
    print('building IV/slippage models from flow CSV (one-time, ~1 min)...', flush=True)
    alerts = sp.load_alerts(sp.FLOW_CSV)
    iv_daily = {s: dict(v) for s, v in sp.build_iv_daily(alerts).items()}
    slip = sp.build_slippage_model(alerts)
    with open(IV_SLIP_CACHE, 'w') as f:
        json.dump({'iv_daily': iv_daily, 'slip': slip}, f)
    return iv_daily, slip


# ------------------------------------------------------------------- orders
class SpreadOrder:
    STRUCTURES = ('bull_put', 'bear_call', 'iron_condor',
                  'long_call', 'long_put', 'calendar')

    def __init__(self, symbol, structure, short_delta=None, strike_offset=None,
                 width=5.0, dte_target=7, far_dte_target=None,
                 tp_pct=None, stop_mult=None, time_exit_dte=None,
                 eod_flatten=False, max_hold_days=None, tag=''):
        assert structure in self.STRUCTURES, structure
        assert (short_delta is None) != (strike_offset is None), \
            'specify exactly one of short_delta / strike_offset'
        self.symbol = symbol
        self.structure = structure
        self.short_delta = short_delta
        self.strike_offset = strike_offset
        self.width = width
        self.dte_target = dte_target
        self.far_dte_target = far_dte_target
        self.tp_pct = tp_pct
        self.stop_mult = stop_mult
        self.time_exit_dte = time_exit_dte
        self.eod_flatten = eod_flatten
        self.max_hold_days = max_hold_days
        self.tag = tag


class MechStrategy:
    name = 'mech'
    symbols = []

    def on_session_start(self, date, ctx):
        pass

    def on_bar(self, dt, bars_so_far, ctx):
        return []


class Ctx:
    """Read-only-ish view handed to strategies."""
    def __init__(self, sim):
        self._sim = sim
        self.date = None
        self.regime = None
        self.leadership = None

    @property
    def balance(self):
        return self._sim.balance

    @property
    def n_open(self):
        return len(self._sim.open_pos)

    def iv(self, symbol):
        return self._sim.iv_for(symbol, self.date)


# -------------------------------------------------------- coverage tracking
class Coverage:
    """Classifies every (contract, day) option-bar lookup by source tier."""
    def __init__(self, polygon):
        self.polygon = polygon
        self.dc = eng.get_disk_cache()
        self.seen = {}
        self.counts = defaultdict(int)
        self.fresh_calls = 0
        self.empty_keys = []

    def _probe(self, key):
        if self.dc.src.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone():
            return 'archive'
        if self.dc.conn.execute("SELECT 1 FROM cache WHERE key=?", (key,)).fetchone():
            return 'overlay'
        return None

    def get_day_bars(self, occ, ds):
        k = (occ, ds)
        if k in self.seen:
            tier, bars = self.seen[k]
            return bars
        key = f"polygon:bars_{occ}_{ds}_{ds}_5_minute"
        tier = self._probe(key)
        before = self.polygon.api_calls
        bars = self.polygon.get_option_bars(occ, ds, ds, multiplier=5,
                                            timespan='minute') or []
        made = self.polygon.api_calls - before
        if tier is None:
            self.fresh_calls += made
            tier = 'api_fresh' if bars else 'api_empty'
            if not bars:
                self.empty_keys.append(key)
            if self.fresh_calls > MAX_FRESH_API_CALLS:
                raise BudgetExceeded(f'{self.fresh_calls} fresh Polygon calls')
        self.counts[tier] += 1
        self.seen[k] = (tier, bars)
        return bars

    def summary(self):
        total = sum(self.counts.values())
        return {'day_keys_requested': total, **dict(self.counts),
                'fresh_api_calls': self.fresh_calls,
                'pct_archive': round(100 * self.counts['archive'] / total, 1) if total else 0,
                'pct_overlay': round(100 * self.counts['overlay'] / total, 1) if total else 0,
                'pct_fresh': round(100 * (self.counts['api_fresh'] + self.counts['api_empty']) / total, 1) if total else 0}


# ------------------------------------------------------------------ simulator
class MechSim:
    def __init__(self, polygon, tradier, iv_daily, slip, closes, regime,
                 start, end, risk_pct=RISK_PCT):
        self.polygon = polygon
        self.tradier = tradier
        self.iv_daily = iv_daily
        self.slip = slip
        self.closes = closes
        self.regime = regime
        self.sessions = ub.trading_sessions(start, end)
        self.coverage = Coverage(polygon)
        self.balance = ACCOUNT_START
        self.risk_dollars = risk_pct * ACCOUNT_START
        self.open_pos = []
        self.trades = []
        self.equity = []
        self.rejects = defaultdict(int)
        self.ctx = Ctx(self)

    # ---------- proxies
    def iv_for(self, symbol, date_str):
        hist = self.iv_daily.get(symbol, {})
        prior = [d for d in hist if d < date_str]
        return hist[max(prior)] if prior else None

    def strike_grid(self, symbol, expiration, spot):
        cached = self.tradier.disk_cache.get(f"tradier:strikes_{symbol}_{expiration}")
        if cached:
            return [float(s) for s in cached]
        if symbol in ETF_DOLLAR_GRID:
            inc = 1.0
        elif spot < 50:
            inc = 1.0
        elif spot < 200:
            inc = 2.5
        elif spot < 500:
            inc = 5.0
        else:
            inc = 10.0
        base = round(spot / inc) * inc
        n = int(spot * 0.12 / inc) + 1
        return [base + i * inc for i in range(-n, n + 1)]

    def pick_expiration(self, date_str, dte_target):
        d = datetime.strptime(date_str, '%Y-%m-%d')
        if dte_target == 0:
            return date_str, 0
        cand = d + timedelta(days=dte_target)
        while cand.weekday() != 4:
            cand += timedelta(days=1)
        while cand.strftime('%Y-%m-%d') in eng.US_MARKET_HOLIDAYS:
            cand -= timedelta(days=1)          # Good-Friday weeks expire Thursday
        return cand.strftime('%Y-%m-%d'), (cand - d).days

    def _delta_dte(self, dte, now_et):
        if dte > 0:
            return float(dte)
        close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return max((close - now_et).total_seconds() / 86400.0, 0.01)

    def _short_by_delta(self, strikes, spot, opt, target, dte_f, sigma):
        cands = [k for k in strikes if (k <= spot if opt == 'P' else k >= spot)]
        best, best_err, best_d = None, 1e9, None
        for k in cands:
            d = sp.bs_delta(spot, k, dte_f, sigma, opt)
            if d is None:
                continue
            err = abs(abs(d) - target)
            if err < best_err:
                best, best_err, best_d = k, err, d
        return best, best_d

    # ---------- leg construction
    def build_legs(self, order, date_str, spot, now_et):
        """Returns (legs, meta) or (None, reject_reason).
        leg = {occ, side(+1 long/-1 short), strike, opt, expiration}"""
        expiration, dte = self.pick_expiration(date_str, order.dte_target)
        strikes = self.strike_grid(order.symbol, expiration, spot)
        if not strikes:
            return None, 'no_strikes'
        sigma = self.iv_for(order.symbol, date_str)
        if not sigma:
            return None, 'no_iv'
        dte_f = self._delta_dte(dte, now_et)
        sym = order.symbol

        def leg(opt, strike, side, exp=expiration):
            return {'occ': eng.build_occ_symbol(sym, exp, opt, strike),
                    'side': side, 'strike': strike, 'opt': opt, 'expiration': exp}

        def short_strike(opt):
            if order.strike_offset is not None:
                tgt = spot - order.strike_offset if opt == 'P' else spot + order.strike_offset
                return eng.find_nearest_strike(tgt, strikes), None
            return self._short_by_delta(strikes, spot, opt, order.short_delta,
                                        dte_f, sigma)

        legs, deltas = [], {}
        st = order.structure
        if st in ('bull_put', 'bear_call', 'iron_condor'):
            if st in ('bull_put', 'iron_condor'):
                ks, d = short_strike('P')
                if ks is None:
                    return None, 'no_short_strike'
                kl = eng.find_nearest_strike(ks - order.width,
                                             [s for s in strikes if s < ks])
                if kl is None:
                    return None, 'no_long_strike'
                legs += [leg('P', ks, -1), leg('P', kl, +1)]
                deltas['put_short'] = d
            if st in ('bear_call', 'iron_condor'):
                ks, d = short_strike('C')
                if ks is None:
                    return None, 'no_short_strike'
                kl = eng.find_nearest_strike(ks + order.width,
                                             [s for s in strikes if s > ks])
                if kl is None:
                    return None, 'no_long_strike'
                legs += [leg('C', ks, -1), leg('C', kl, +1)]
                deltas['call_short'] = d
            widths = []
            ps = [l for l in legs if l['opt'] == 'P']
            cs = [l for l in legs if l['opt'] == 'C']
            if ps:
                widths.append(abs(ps[0]['strike'] - ps[1]['strike']))
            if cs:
                widths.append(abs(cs[0]['strike'] - cs[1]['strike']))
            if any(w <= 0 for w in widths):
                return None, 'zero_width'
            meta = {'is_credit': True, 'max_width': max(widths),
                    'expiration': expiration, 'dte': dte, 'deltas': deltas,
                    'sigma': sigma}
            return legs, meta
        if st in ('long_call', 'long_put'):
            opt = 'C' if st == 'long_call' else 'P'
            ks, d = short_strike(opt)   # same targeting helper, leg held long
            if ks is None:
                return None, 'no_strike'
            legs = [leg(opt, ks, +1)]
            return legs, {'is_credit': False, 'max_width': None,
                          'expiration': expiration, 'dte': dte,
                          'deltas': {'long': d}, 'sigma': sigma}
        if st == 'calendar':
            far_exp, far_dte = self.pick_expiration(date_str,
                                                    order.far_dte_target or
                                                    order.dte_target + 28)
            tgt = spot + (order.strike_offset or 0.0)
            k = eng.find_nearest_strike(tgt, strikes)
            if k is None or far_exp <= expiration:
                return None, 'bad_calendar'
            legs = [leg('P' if order.short_delta and order.short_delta < 0 else 'C', k, -1),
                    leg('P' if order.short_delta and order.short_delta < 0 else 'C',
                        k, +1, exp=far_exp)]
            return legs, {'is_credit': False, 'max_width': None,
                          'expiration': expiration, 'dte': dte,
                          'far_expiration': far_exp, 'deltas': {}, 'sigma': sigma}
        return None, 'bad_structure'

    # ---------- pricing series
    def aligned_bars(self, legs, start_date, end_date):
        """Intersection-aligned per-bar structure values, fetched per day.
        value = sum(side * close); credit structures have value < 0, and
        cost-to-close = -value (matches sim_proposed's short-long spread)."""
        sessions = ub.trading_sessions(start_date, end_date)
        per_leg = []
        for l in legs:
            by_ts = {}
            for ds in sessions:
                if ds > l['expiration']:
                    continue
                for b in self.coverage.get_day_bars(l['occ'], ds):
                    by_ts[b['t']] = b['c']
            per_leg.append(by_ts)
        common = set(per_leg[0])
        for m in per_leg[1:]:
            common &= set(m)
        out = []
        for ts in sorted(common):
            et = ts_et(ts)
            if not (dtime(9, 30) <= et.time() < dtime(16, 5)):
                continue
            out.append({'ts': ts, 'date': et.strftime('%Y-%m-%d'),
                        'time_et': et.strftime('%H:%M'),
                        'value': sum(l['side'] * m[ts]
                                     for l, m in zip(legs, per_leg))})
        return out

    # ---------- intrinsic settlement
    def _underlying_close(self, symbol, date_str):
        und = self.closes.get(symbol, {})
        if date_str in und:
            return und[date_str]
        prior = [d for d in sorted(und) if d <= date_str]
        if prior:
            return und[prior[-1]]
        bars = rth(ub.get_bars(symbol, date_str))
        return bars[-1]['c'] if bars else None

    def _intrinsic_value(self, legs, u):
        v = 0.0
        for l in legs:
            intr = max(0.0, l['strike'] - u) if l['opt'] == 'P' else \
                   max(0.0, u - l['strike'])
            v += l['side'] * intr
        return v

    # ---------- exit resolution (deterministic once entered)
    def resolve_exit(self, pos):
        o = pos['order']
        legs, meta = pos['legs'], pos['meta']
        is_credit = meta['is_credit']
        slip_total = pos['slip_leg'] * len(legs)
        credit, debit = pos.get('credit'), pos.get('debit')
        exp = meta['expiration']
        exp_dt = datetime.strptime(exp, '%Y-%m-%d')
        flatten_ts = et_ms(pos['entry_date'], EOD_FLATTEN_ET.hour,
                           EOD_FLATTEN_ET.minute) if o.eod_flatten else None
        entry_d = datetime.strptime(pos['entry_date'], '%Y-%m-%d')

        tp_level = stop_level = None
        if is_credit:
            if o.tp_pct is not None:
                tp_level = credit * (1.0 - o.tp_pct)
            if o.stop_mult is not None:
                stop_level = credit * (1.0 + o.stop_mult)
        else:
            if o.tp_pct is not None:
                tp_level = debit * (1.0 + o.tp_pct)
            if o.stop_mult is not None:
                stop_level = debit * (1.0 - o.stop_mult)

        def exit_px(cost, clamp=True):
            """Marketable close price incl. slippage.
            credit: pay cost + slip to buy back; debit: receive value - slip."""
            if is_credit:
                return (max(0.0, cost) if clamp else cost) + slip_total
            return max(0.0, cost - slip_total)

        last_entry_day_bar = None
        for b in pos['bars']:
            if b['ts'] <= pos['entry_ts']:
                continue
            cost = -b['value'] if is_credit else b['value']   # close @ this
            bar_d = datetime.strptime(b['date'], '%Y-%m-%d')
            dte_now = (exp_dt - bar_d).days
            if flatten_ts and b['date'] == pos['entry_date']:
                last_entry_day_bar = b
            if flatten_ts and b['ts'] >= flatten_ts:
                return b, exit_px(cost), 'eod_flatten'
            if o.time_exit_dte is not None and dte_now <= o.time_exit_dte:
                return b, exit_px(cost), f'time_exit_{o.time_exit_dte}dte'
            if o.max_hold_days is not None and (bar_d - entry_d).days >= o.max_hold_days:
                return b, exit_px(cost), 'max_hold'
            if is_credit:
                if stop_level is not None and cost >= stop_level:
                    return b, exit_px(cost, clamp=False), 'stop'
                if tp_level is not None and 0 <= cost <= tp_level:
                    return b, exit_px(cost, clamp=False), 'tp'
            else:
                if stop_level is not None and cost <= stop_level:
                    return b, exit_px(cost), 'stop'
                if tp_level is not None and cost >= tp_level:
                    return b, exit_px(cost), 'tp'

        # bars exhausted before any trigger
        if flatten_ts and last_entry_day_bar is not None:
            b = last_entry_day_bar
            cost = -b['value'] if is_credit else b['value']
            return b, exit_px(cost), 'eod_flatten_lastbar'
        # intrinsic settlement at (near) expiration — cash settle, no slip
        u = self._underlying_close(pos['symbol'], exp)
        if u is not None:
            v = self._intrinsic_value(legs, u)
            cost = -v if is_credit else v
            if is_credit and meta['max_width']:
                cost = min(meta['max_width'], max(0.0, cost))
            else:
                cost = max(0.0, cost)
            fake = {'ts': et_ms(exp, 16, 0), 'date': exp, 'time_et': '16:00',
                    'value': v}
            return fake, cost, 'expiration_intrinsic'
        if pos['bars']:
            b = pos['bars'][-1]
            cost = -b['value'] if is_credit else b['value']
            return b, exit_px(cost), 'last_bar_fallback'
        fake = {'ts': et_ms(exp, 16, 0), 'date': exp, 'time_et': '16:00',
                'value': 0.0}
        return fake, 0.0, 'no_data_assume_worthless'

    # ---------- entry
    def try_enter(self, order, date_str, signal_ts, spot, strategy_name):
        if len(self.open_pos) >= MAX_CONCURRENT:
            self.rejects['max_concurrent'] += 1
            return None
        now_et = ts_et(signal_ts)
        legs, meta = self.build_legs(order, date_str, spot, now_et)
        if legs is None:
            self.rejects[meta] += 1
            return None
        bars = self.aligned_bars(legs, date_str, meta['expiration'])
        cutoff = signal_ts + ENTRY_FILL_WINDOW_MIN * 60_000
        entry_bar = next((b for b in bars
                          if b['date'] == date_str and signal_ts <= b['ts'] <= cutoff),
                         None)
        if entry_bar is None:
            self.rejects['no_pricing'] += 1
            return None
        slip_leg = self.slip.get(order.symbol, SLIP_FLOOR)
        slip_total = slip_leg * len(legs)
        pos = {'order': order, 'legs': legs, 'meta': meta, 'bars': bars,
               'symbol': order.symbol, 'structure': order.structure,
               'strategy': strategy_name, 'entry_date': date_str,
               'entry_ts': entry_bar['ts'], 'entry_time_et': entry_bar['time_et'],
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
        commission = COMMISSION_PER_LEG_RT * len(legs) * qty
        if meta['is_credit']:
            pos['pnl'] = (pos['credit'] - cost) * qty * 100.0 - commission
        else:
            # for debit structures resolve_exit returns slip-adjusted proceeds
            pos['pnl'] = (cost - pos['debit']) * qty * 100.0 - commission
        self.open_pos.append(pos)
        return pos

    # ---------- main loop
    def run(self, strategies):
        all_syms = sorted({s for st in strategies for s in st.symbols})
        for d_i, date_str in enumerate(self.sessions):
            self.ctx.date = date_str
            self.ctx.regime, self.ctx.leadership = self.regime.get(date_str,
                                                                   (None, None))
            bars_by_sym = {s: rth(ub.get_bars(s, date_str)) for s in all_syms}
            clock = sorted({b['t'] for v in bars_by_sym.values() for b in v})
            if not clock:
                self.equity.append((date_str, self.balance))
                continue
            for st in strategies:
                st.on_session_start(date_str, self.ctx)
            day_start_bal = self.balance
            realized_today = 0.0
            halted = False
            idx = {s: 0 for s in all_syms}
            so_far = {s: [] for s in all_syms}
            print(f"  .. {date_str} ({d_i+1}/{len(self.sessions)}) "
                  f"bal={self.balance:.0f} trades={len(self.trades)} "
                  f"fresh_api={self.coverage.fresh_calls}", flush=True)

            for ts in clock:
                for s in all_syms:
                    v = bars_by_sym[s]
                    while idx[s] < len(v) and v[idx[s]]['t'] <= ts:
                        so_far[s].append(v[idx[s]])
                        idx[s] += 1
                signal_ts = ts + 300_000          # bar completes 5 min after start
                # exits due before this moment
                for p in [p for p in self.open_pos
                          if p['exit_bar']['date'] <= date_str
                          and p['exit_bar']['ts'] <= signal_ts]:
                    self._close(p)
                    self.open_pos.remove(p)
                    realized_today += p['pnl']
                if not halted and realized_today <= -DAILY_LOSS_HALT * day_start_bal:
                    halted = True
                dt = ts_et(signal_ts)
                for st in strategies:
                    orders = st.on_bar(dt, so_far, self.ctx) or []
                    for o in orders:
                        if o is None:
                            continue
                        if halted:
                            self.rejects['daily_halt'] += 1
                            continue
                        spot_bars = so_far.get(o.symbol) or []
                        if not spot_bars:
                            self.rejects['no_underlying'] += 1
                            continue
                        self.try_enter(o, date_str, signal_ts,
                                       spot_bars[-1]['c'], st.name)
            # end-of-day sweep
            for p in [p for p in self.open_pos if p['exit_bar']['date'] <= date_str]:
                self._close(p)
                self.open_pos.remove(p)
                realized_today += p['pnl']
            self.equity.append((date_str, self.balance))
        for p in list(self.open_pos):
            self._close(p)
            self.open_pos.remove(p)
        return self.summarize()

    def _close(self, p):
        self.balance += p['pnl']
        self.trades.append(p)

    # ---------- reporting
    def summarize(self):
        t = self.trades
        wins = [x for x in t if x['pnl'] > 0]
        losses = [x for x in t if x['pnl'] <= 0]
        gw = sum(x['pnl'] for x in wins)
        gl = -sum(x['pnl'] for x in losses)
        peak, maxdd = ACCOUNT_START, 0.0
        for _, bal in self.equity:
            peak = max(peak, bal)
            maxdd = max(maxdd, peak - bal)
        by_reason = defaultdict(lambda: [0, 0.0])
        for x in t:
            by_reason[x['exit_reason']][0] += 1
            by_reason[x['exit_reason']][1] += round(x['pnl'], 2)
        return {'n_trades': len(t), 'wins': len(wins), 'losses': len(losses),
                'win_rate': round(len(wins) / len(t), 3) if t else 0.0,
                'avg_win': round(gw / len(wins), 2) if wins else 0.0,
                'avg_loss': round(-gl / len(losses), 2) if losses else 0.0,
                'profit_factor': round(gw / gl, 3) if gl > 0 else (float('inf') if gw > 0 else 0.0),
                'total_pnl': round(sum(x['pnl'] for x in t), 2),
                'end_balance': round(self.balance, 2),
                'max_drawdown': round(maxdd, 2),
                'by_exit_reason': {k: [v[0], round(v[1], 2)] for k, v in sorted(by_reason.items())},
                'rejects': dict(self.rejects),
                'coverage': self.coverage.summary()}

    def dump_trades(self, path):
        cols = ['strategy', 'symbol', 'structure', 'entry_date', 'entry_time_et',
                'expiration', 'dte', 'legs', 'deltas', 'spot', 'sigma',
                'credit', 'debit', 'qty', 'slip_leg', 'n_aligned_bars',
                'exit_date', 'exit_time_et', 'exit_cost', 'exit_reason', 'pnl']
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(cols)
            for p in self.trades:
                legs_s = ' / '.join(f"{'S' if l['side'] < 0 else 'L'}"
                                    f"{l['opt']}{l['strike']:g}" for l in p['legs'])
                deltas_s = ' '.join(f"{k}={v:.3f}" for k, v in p['deltas'].items()
                                    if v is not None)
                w.writerow([p['strategy'], p['symbol'], p['structure'],
                            p['entry_date'], p['entry_time_et'], p['expiration'],
                            p['dte'], legs_s, deltas_s, round(p['spot'], 2),
                            round(p['sigma'], 4),
                            round(p.get('credit', 0), 3) or '',
                            round(p.get('debit', 0), 3) or '',
                            p['qty'], p['slip_leg'], p['n_aligned_bars'],
                            p['exit_bar']['date'], p['exit_bar']['time_et'],
                            round(p['exit_cost'], 3), p['exit_reason'],
                            round(p['pnl'], 2)])


# ------------------------------------------------------- reference strategies
class SpyPutwriteWeekly(MechStrategy):
    """Every Monday 10:00 ET: sell SPY 30-delta put spread, $5 wide,
    nearest weekly expiry >= 4 DTE, TP 50%, stop 2.0x, exit 1 DTE."""
    name = 'spy_putwrite_weekly'
    symbols = ['SPY']

    def on_session_start(self, date, ctx):
        self.fired = False
        self.is_monday = datetime.strptime(date, '%Y-%m-%d').weekday() == 0

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or not self.is_monday or dt.time() < dtime(10, 0):
            return []
        self.fired = True
        return [SpreadOrder('SPY', 'bull_put', short_delta=0.30, width=5.0,
                            dte_target=4, tp_pct=0.50, stop_mult=2.0,
                            time_exit_dte=1, tag='putwrite')]


class Qqq0dteIc1100(MechStrategy):
    """Daily 11:00 ET: sell QQQ 0DTE iron condor, ~15-delta shorts, $5 wings,
    EOD flatten, no TP/stop (hold to close)."""
    name = 'qqq_0dte_ic_1100'
    symbols = ['QQQ']

    def on_session_start(self, date, ctx):
        self.fired = False

    def on_bar(self, dt, bars_so_far, ctx):
        if self.fired or dt.time() < dtime(11, 0):
            return []
        self.fired = True
        return [SpreadOrder('QQQ', 'iron_condor', short_delta=0.15, width=5.0,
                            dte_target=0, eod_flatten=True, tag='0dte_ic')]


REFERENCE = {s.name: s for s in (SpyPutwriteWeekly(), Qqq0dteIc1100())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2025-06-01')
    ap.add_argument('--end', default='2025-06-30')
    ap.add_argument('--strategy', default='both',
                    choices=list(REFERENCE) + ['both'])
    ap.add_argument('--tag', default='mech_smoke')
    args = ap.parse_args()

    iv_daily, slip = load_iv_slip()
    closes = sp.load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    regime = sp.load_regime(os.path.join(HERE, 'regime_series.csv'))

    polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    polygon.min_interval = 0.30
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)

    names = list(REFERENCE) if args.strategy == 'both' else [args.strategy]
    results = {}
    for name in names:
        print(f"\n=== {name} | {args.start}..{args.end} ===", flush=True)
        sim = MechSim(polygon, tradier, iv_daily, slip, closes, regime,
                      args.start, args.end)
        try:
            summary = sim.run([REFERENCE[name]])
        except BudgetExceeded as e:
            print(f"!! ABORT: API budget exceeded ({e})", flush=True)
            summary = sim.summarize()
            summary['aborted'] = str(e)
        sim.dump_trades(os.path.join(HERE, f'trades_{args.tag}_{name}.csv'))
        results[name] = summary
        print(json.dumps(summary, indent=2))

    with open(os.path.join(HERE, f'summary_{args.tag}.json'), 'w') as f:
        json.dump(results, f, indent=2)
    dc = eng.get_disk_cache()
    print('\ncache:', dc.stats(), '| polygon api calls total:',
          polygon.api_calls, '| underlying api calls:', ub.api_calls[0],
          flush=True)


if __name__ == '__main__':
    main()
