#!/usr/bin/env python3
"""Proposed-rules credit-spread simulator ("fable-options credit_spreads leg").

Replays the same UW/whalestream flow-alert signal stream the legacy engine
uses (engine_v345 SignalAggregator, identical thresholds), then applies the
PROPOSED rule set on top:

  Entry gates
    - universe: SPY/QQQ/IWM + mega-caps (AAPL MSFT AMZN GOOGL META NVDA TSLA)
    - flow signal bullish -> bull put spread, ONLY if regime != DOWN and IVR >= 35
    - flow signal bearish -> bear call spread, ONLY if regime == DOWN and DEFENSIVE_LED
    - short strike: max-premium strike with BS |delta| <= cap (default 0.30)
      sigma = per-symbol intraday median alert IV (point-in-time)
    - DTE 7-21 (first weekly Friday >= 7 days out), credit >= 25% of width
  Exits
    - TP: buy back at tp_pct (default 50%) of credit
    - STOP: loss >= stop_mult x credit (default 1.0x), i.e. spread >= (1+stop)x credit
    - time exit at 2 DTE; expiration intrinsic settlement as fallback
  Portfolio
    - $10k, risk $500/trade (5%), qty = floor(500/max_loss), min 1, max 3 concurrent
    - 2% daily-loss halt on new entries; 5-session same-ticker cooldown after a loss
  Costs
    - empirical NBBO half-spread per leg (per-symbol median from whalestream
      bid/ask on comparable OTM contracts), floor $0.05/leg; x2 legs at entry
      and exit. $1.30 per spread round-trip commission per contract.

Pricing backbone: Polygon 5-min aligned spread bars via the archived disk
cache (read-only) + local overlay, identical to the legacy engine.
"""
import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v345 as eng  # reuses PolygonClient, TradierClient, SignalAggregator, helpers

UNIVERSE = {'SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'META', 'NVDA', 'TSLA'}
R_RATE = 0.045
COMMISSION_RT = 1.30          # per spread, per contract, round trip
ACCOUNT_START = 10_000.0
RISK_PER_TRADE = 500.0        # 5% of $10k
MAX_CONCURRENT = 3
DAILY_LOSS_HALT = 0.02
COOLDOWN_SESSIONS = 5
MIN_CREDIT_FRAC = 0.25
TIME_EXIT_DTE = 2
MIN_IVR_HISTORY = 20
SLIP_FLOOR = 0.05

FLOW_CSV = os.path.join(HERE, 'whalestream_flow_2025-03_2026-02.csv')


# ---------------------------------------------------------------- BS helpers
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, dte_days, sigma, opt_type):
    if spot <= 0 or strike <= 0 or sigma <= 0 or dte_days <= 0:
        return None
    T = dte_days / 365.0
    d1 = (math.log(spot / strike) + (R_RATE + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if opt_type == 'C' else _ncdf(d1) - 1.0


# ---------------------------------------------------------------- data load
class Alert(eng.FlowAlert):
    """FlowAlert + delta/iv extras from the converted whalestream CSV."""
    def __init__(self, row):
        super().__init__(row)
        def fl(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        self.delta = fl(row.get('Delta'))
        self.iv = fl(row.get('IV'))
        self.ba_spread = fl(row.get('BidAskSpread'))
        self.exp_days = fl(row.get('ExpiresInDays'))


def load_alerts(path):
    by_date = defaultdict(list)
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            try:
                a = Alert(row)
            except Exception:
                continue
            by_date[a.date_key].append(a)
    for v in by_date.values():
        v.sort(key=lambda a: a.timestamp)
    return dict(by_date)


def load_regime(path):
    out = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            out[row['date']] = (row['regime'], row['leadership'])
    return out


def load_underlying(path):
    closes = defaultdict(dict)   # symbol -> date -> close
    with open(path) as f:
        r = csv.reader(f)
        header = next(r)
        syms = header[1:]
        for row in r:
            d = row[0][:10]
            for s, v in zip(syms, row[1:]):
                if v:
                    closes[s][d] = float(v)
    return closes


# ---------------------------------------------------- IV proxy + slippage
def build_iv_daily(alerts_by_date):
    """median alert IV per (symbol, date) — full-day medians, used as the
    trailing history for IV rank (only days strictly BEFORE the decision day
    are consulted, so full-day medians are point-in-time safe)."""
    iv_daily = defaultdict(dict)
    for d, alerts in alerts_by_date.items():
        bucket = defaultdict(list)
        for a in alerts:
            s = a.effective_symbol
            if s in UNIVERSE and a.iv and 0.03 < a.iv < 4.0:
                bucket[s].append(a.iv)
        for s, vals in bucket.items():
            vals.sort()
            iv_daily[s][d] = vals[len(vals) // 2]
    return iv_daily


def build_slippage_model(alerts_by_date):
    """Per-symbol median half-spread from whalestream NBBO on comparable
    contracts (OTM-ish |delta| 0.05-0.40, DTE 4-25). Floor $0.05."""
    pools = defaultdict(list)
    for alerts in alerts_by_date.values():
        for a in alerts:
            s = a.effective_symbol
            if s not in UNIVERSE or a.ba_spread is None or a.delta is None:
                continue
            if a.exp_days is None or not (4 <= a.exp_days <= 25):
                continue
            if not (0.05 <= abs(a.delta) <= 0.40):
                continue
            if 0 < a.ba_spread < 5:
                # index alerts (SPX/NDX/RUT) have index-scale spreads; skip
                if a.symbol in eng.INDEX_TO_ETF:
                    continue
                pools[s].append(a.ba_spread / 2.0)
    model = {}
    for s, vals in pools.items():
        vals.sort()
        model[s] = max(SLIP_FLOOR, vals[len(vals) // 2])
    return model


# ---------------------------------------------------------------- simulator
class ProposedSim:
    def __init__(self, polygon, tradier, alerts_by_date, regime, iv_daily,
                 slip_model, closes, cfg):
        self.polygon = polygon
        self.tradier = tradier
        self.alerts_by_date = alerts_by_date
        self.regime = regime
        self.iv_daily = iv_daily
        self.slip = slip_model
        self.closes = closes
        self.cfg = cfg
        self.sessions = sorted(d for d in alerts_by_date
                               if cfg['start'] <= d <= cfg['end'])
        self.session_idx = {d: i for i, d in enumerate(self.sessions)}
        self.balance = ACCOUNT_START
        self.trades = []          # closed trades
        self.equity = []          # (date, balance) realized equity
        self.reject_counts = defaultdict(int)

    # ---------- point-in-time IV rank
    def iv_rank(self, symbol, date_str, scan_time, alerts_today):
        hist_dates = [d for d in sorted(self.iv_daily.get(symbol, {})) if d < date_str]
        hist = [self.iv_daily[symbol][d] for d in hist_dates[-60:]]
        if len(hist) < MIN_IVR_HISTORY:
            return None
        cur_vals = sorted(a.iv for a in alerts_today
                          if a.effective_symbol == symbol and a.timestamp <= scan_time
                          and a.iv and 0.03 < a.iv < 4.0)
        if len(cur_vals) >= 3:
            cur = cur_vals[len(cur_vals) // 2]
        elif hist_dates:
            cur = self.iv_daily[symbol][hist_dates[-1]]
        else:
            return None
        rank = 100.0 * sum(1 for h in hist if h < cur) / len(hist)
        return rank, cur

    # ---------- strike selection
    def pick_spread(self, symbol, current_date, spot, direction, sigma):
        target_exp = eng.get_weekly_expiration(current_date, 7, 21)
        if not target_exp:
            return None
        dte = (datetime.strptime(target_exp, '%Y-%m-%d') - current_date).days
        strikes = self.tradier.get_strikes(symbol, target_exp)
        if not strikes or len(strikes) < 5:
            if spot < 50: inc = 1
            elif spot < 200: inc = 2.5
            elif spot < 500: inc = 5
            else: inc = 10
            base = round(spot / inc) * inc
            strikes = [base + i * inc for i in range(-30, 31)]
        cap = self.cfg['delta_cap']
        width = max(3, min(10, spot * 0.01))
        if direction == 'bullish':
            cands = sorted([s for s in strikes if s < spot], reverse=True)
            short_strike = None
            for k in cands:
                d = bs_delta(spot, k, dte, sigma, 'P')
                if d is not None and abs(d) <= cap:
                    short_strike = k
                    short_delta = d
                    break
            if short_strike is None:
                return None
            long_strike = eng.find_nearest_strike(short_strike - width,
                                                  [s for s in strikes if s < short_strike])
            if not long_strike:
                return None
            w = short_strike - long_strike
            occ_s = eng.build_occ_symbol(symbol, target_exp, 'P', short_strike)
            occ_l = eng.build_occ_symbol(symbol, target_exp, 'P', long_strike)
            opt = 'P'
        else:
            cands = sorted([s for s in strikes if s > spot])
            short_strike = None
            for k in cands:
                d = bs_delta(spot, k, dte, sigma, 'C')
                if d is not None and abs(d) <= cap:
                    short_strike = k
                    short_delta = d
                    break
            if short_strike is None:
                return None
            long_strike = eng.find_nearest_strike(short_strike + width,
                                                  [s for s in strikes if s > short_strike])
            if not long_strike:
                return None
            w = long_strike - short_strike
            occ_s = eng.build_occ_symbol(symbol, target_exp, 'C', short_strike)
            occ_l = eng.build_occ_symbol(symbol, target_exp, 'C', long_strike)
            opt = 'C'
        if w <= 0:
            return None
        return {'expiration': target_exp, 'dte': dte, 'short': short_strike,
                'long': long_strike, 'width': w, 'occ_s': occ_s, 'occ_l': occ_l,
                'opt': opt, 'short_delta': short_delta}

    # ---------- spread bars
    def spread_bars(self, occ_s, occ_l, entry_date, expiration):
        sb = self.polygon.get_option_bars(occ_s, entry_date, expiration,
                                          multiplier=5, timespan='minute') or []
        lb = self.polygon.get_option_bars(occ_l, entry_date, expiration,
                                          multiplier=5, timespan='minute') or []
        s_by = {b['t']: b for b in sb}
        l_by = {b['t']: b for b in lb}
        out = []
        for ts in sorted(set(s_by) & set(l_by)):
            out.append({'ts': ts, 'date': eng.ts_to_date(ts),
                        'time_et': eng.ts_to_time(ts),
                        'spread': s_by[ts]['c'] - l_by[ts]['c']})
        return out

    # ---------- exit resolution (deterministic per position once entered)
    def resolve_exit(self, pos):
        cfg = self.cfg
        credit = pos['credit']
        tp_level = credit * (1.0 - cfg['tp_pct'])          # buy back at this spread value
        stop_level = credit * (1.0 + cfg['stop_mult']) if cfg['stop_mult'] else None
        exp_dt = datetime.strptime(pos['expiration'], '%Y-%m-%d')
        slip2 = pos['slip_leg'] * 2

        for b in pos['bars']:
            if b['ts'] <= pos['entry_ts']:
                continue
            bar_dt = datetime.strptime(b['date'], '%Y-%m-%d')
            dte_now = (exp_dt - bar_dt).days
            if dte_now <= TIME_EXIT_DTE:
                cost = max(0.0, b['spread']) + slip2
                return b, cost, 'time_exit_2dte'
            if stop_level is not None and b['spread'] >= stop_level:
                cost = b['spread'] + slip2
                return b, cost, 'stop'
            if b['spread'] <= tp_level and b['spread'] >= 0:
                cost = b['spread'] + slip2
                return b, cost, 'tp'
        # ran to expiration without bars triggering: intrinsic settlement
        exp_d = pos['expiration']
        und = self.closes.get(pos['symbol'], {})
        u = und.get(exp_d)
        if u is None:
            prior = [d for d in sorted(und) if d <= exp_d]
            u = und[prior[-1]] if prior else None
        if u is not None:
            if pos['opt'] == 'P':
                intr_s = max(0.0, pos['short'] - u)
                intr_l = max(0.0, pos['long'] - u)
            else:
                intr_s = max(0.0, u - pos['short'])
                intr_l = max(0.0, u - pos['long'])
            cost = min(pos['width'], max(0.0, intr_s - intr_l))
            fake = {'ts': int(exp_dt.replace(hour=21, tzinfo=timezone.utc).timestamp() * 1000),
                    'date': exp_d, 'time_et': '16:00', 'spread': cost}
            return fake, cost, 'expiration_intrinsic'
        if pos['bars']:
            last = pos['bars'][-1]
            return last, max(0.0, last['spread']) + slip2, 'last_bar_fallback'
        fake = {'ts': int(exp_dt.replace(hour=21, tzinfo=timezone.utc).timestamp() * 1000),
                'date': exp_d, 'time_et': '16:00', 'spread': 0.0}
        return fake, 0.0, 'no_data_assume_expired_otm'

    # ---------- main loop
    def run(self):
        cfg = self.cfg
        open_pos = []                  # positions w/ scheduled exits
        cooldown_until = {}            # symbol -> session idx blocked through
        agg_cls = eng.SignalAggregator

        for d_i, date_str in enumerate(self.sessions):
            if d_i % 5 == 0:
                print(f"  .. {date_str} ({d_i}/{len(self.sessions)}) bal={self.balance:.0f} "
                      f"trades={len(self.trades)}", flush=True)
            alerts = self.alerts_by_date[date_str]
            day_start_balance = self.balance
            realized_today = 0.0
            halted = False
            current_date = datetime.strptime(date_str, '%Y-%m-%d')
            reg, lead = self.regime.get(date_str, (None, None))

            aggregator = agg_cls()
            for a in alerts:
                aggregator.add_alert(a)

            # pending exit events for today (from positions opened earlier)
            def due_exits(upto_ts):
                return [p for p in open_pos
                        if p['exit_bar']['date'] <= date_str and p['exit_bar']['ts'] <= upto_ts]

            day_start = alerts[0].timestamp.replace(hour=14, minute=30, second=0) if alerts \
                else current_date.replace(hour=14, minute=30)
            day_end = alerts[0].timestamp.replace(hour=20, minute=30, second=0) if alerts \
                else current_date.replace(hour=20, minute=30)

            scan_time = day_start
            traded_today = set()
            while scan_time <= day_end:
                scan_ts = int(scan_time.replace(tzinfo=timezone.utc).timestamp() * 1000)
                # process exits due before this scan
                for p in due_exits(scan_ts):
                    self._close(p, cooldown_until)
                    open_pos.remove(p)
                    realized_today += p['pnl']
                if not halted and realized_today <= -DAILY_LOSS_HALT * day_start_balance:
                    halted = True
                if halted or reg is None:
                    scan_time += timedelta(minutes=5)
                    continue

                actionable = aggregator.get_actionable(scan_time)
                for symbol, profile in actionable:
                    if len(open_pos) >= MAX_CONCURRENT:
                        break
                    if symbol not in UNIVERSE:
                        self.reject_counts['universe'] += 1
                        continue
                    if symbol in traded_today or any(p['symbol'] == symbol for p in open_pos):
                        continue
                    if cooldown_until.get(symbol, -1) >= d_i:
                        self.reject_counts['cooldown'] += 1
                        continue
                    spot = profile['spot_price']
                    if spot <= 0:
                        continue
                    direction = profile['direction']

                    # --- regime gate
                    if cfg['use_regime_gate']:
                        if direction == 'bullish' and reg == 'DOWN':
                            self.reject_counts['regime_bps_in_down'] += 1
                            continue
                        if direction == 'bearish' and not (reg == 'DOWN' and lead == 'DEFENSIVE_LED'):
                            self.reject_counts['regime_bcs_gate'] += 1
                            continue
                    # --- IV rank gate (applies to BPS entries per spec)
                    ivr_info = self.iv_rank(symbol, date_str, scan_time, alerts)
                    if ivr_info is None:
                        self.reject_counts['no_iv_history'] += 1
                        continue
                    ivr, cur_iv = ivr_info
                    if cfg['use_iv_filter'] and direction == 'bullish' and ivr < 35.0:
                        self.reject_counts['ivr_below_35'] += 1
                        continue

                    spread = self.pick_spread(symbol, current_date, spot, direction, cur_iv)
                    if not spread:
                        self.reject_counts['no_spread'] += 1
                        continue
                    bars = self.spread_bars(spread['occ_s'], spread['occ_l'],
                                            date_str, spread['expiration'])
                    entry_bar = next((b for b in bars
                                      if b['date'] == date_str and b['ts'] >= scan_ts), None)
                    if entry_bar is None:
                        self.reject_counts['no_pricing'] += 1
                        continue
                    slip_leg = self.slip.get(symbol, SLIP_FLOOR)
                    credit = entry_bar['spread'] - 2 * slip_leg
                    if credit <= 0:
                        self.reject_counts['credit_wiped'] += 1
                        continue
                    if credit < MIN_CREDIT_FRAC * spread['width']:
                        self.reject_counts['credit_lt_25pct'] += 1
                        continue
                    if credit >= spread['width']:
                        self.reject_counts['credit_gt_width'] += 1
                        continue
                    max_loss_per = (spread['width'] - credit) * 100.0
                    qty = max(1, int(RISK_PER_TRADE // max_loss_per))

                    pos = {
                        'symbol': symbol, 'direction': direction,
                        'entry_date': date_str, 'entry_time_et': entry_bar['time_et'],
                        'entry_ts': entry_bar['ts'],
                        'expiration': spread['expiration'], 'dte': spread['dte'],
                        'short': spread['short'], 'long': spread['long'],
                        'width': spread['width'], 'opt': spread['opt'],
                        'short_delta': spread['short_delta'],
                        'credit': credit, 'raw_mid': entry_bar['spread'],
                        'slip_leg': slip_leg, 'qty': qty, 'bars': bars,
                        'spot': spot, 'ivr': ivr, 'iv_used': cur_iv,
                        'regime': reg, 'leadership': lead,
                    }
                    bar, cost, reason = self.resolve_exit(pos)
                    pos['exit_bar'] = bar
                    pos['exit_cost'] = cost
                    pos['exit_reason'] = reason
                    pos['pnl'] = (credit - cost) * qty * 100.0 - COMMISSION_RT * qty
                    open_pos.append(pos)
                    traded_today.add(symbol)
                scan_time += timedelta(minutes=5)

            # end of day: process any remaining exits dated today
            for p in [p for p in open_pos if p['exit_bar']['date'] <= date_str]:
                self._close(p, cooldown_until)
                open_pos.remove(p)
                realized_today += p['pnl']
            self.equity.append((date_str, self.balance))

        # force-close stragglers at end of window
        for p in open_pos:
            self._close(p, cooldown_until)
        return self.summarize()

    def _close(self, p, cooldown_until):
        self.balance += p['pnl']
        if p['pnl'] < 0:
            idx = self.session_idx.get(p['exit_bar']['date'])
            if idx is None:
                later = [i for d, i in self.session_idx.items() if d >= p['exit_bar']['date']]
                idx = min(later) if later else len(self.sessions) - 1
            cooldown_until[p['symbol']] = idx + COOLDOWN_SESSIONS
        self.trades.append(p)

    # ---------- reporting
    def summarize(self):
        t = self.trades
        wins = [x for x in t if x['pnl'] > 0]
        losses = [x for x in t if x['pnl'] <= 0]
        gw = sum(x['pnl'] for x in wins)
        gl = -sum(x['pnl'] for x in losses)
        # max drawdown on realized equity curve
        peak = ACCOUNT_START
        maxdd = 0.0
        for _, bal in self.equity:
            peak = max(peak, bal)
            maxdd = max(maxdd, peak - bal)
        monthly = defaultdict(float)
        for x in t:
            monthly[x['exit_bar']['date'][:7]] += x['pnl']
        by_regime = defaultdict(lambda: [0, 0.0])
        for x in t:
            by_regime[(x['regime'], x['direction'])][0] += 1
            by_regime[(x['regime'], x['direction'])][1] += x['pnl']
        return {
            'n_trades': len(t),
            'wins': len(wins), 'losses': len(losses),
            'win_rate': len(wins) / len(t) if t else 0.0,
            'avg_win': gw / len(wins) if wins else 0.0,
            'avg_loss': -gl / len(losses) if losses else 0.0,
            'profit_factor': (gw / gl) if gl > 0 else float('inf') if gw > 0 else 0.0,
            'total_pnl': sum(x['pnl'] for x in t),
            'end_balance': self.balance,
            'max_drawdown': maxdd,
            'monthly': dict(sorted(monthly.items())),
            'by_regime': {f"{k[0]}|{k[1]}": v for k, v in sorted(by_regime.items())},
            'rejects': dict(self.reject_counts),
        }

    def dump_trades(self, path):
        cols = ['symbol', 'direction', 'entry_date', 'entry_time_et', 'expiration',
                'dte', 'short', 'long', 'width', 'opt', 'short_delta', 'credit',
                'raw_mid', 'slip_leg', 'qty', 'spot', 'ivr', 'iv_used', 'regime',
                'leadership', 'exit_date', 'exit_time_et', 'exit_cost',
                'exit_reason', 'pnl']
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(cols)
            for p in self.trades:
                w.writerow([p['symbol'], p['direction'], p['entry_date'],
                            p['entry_time_et'], p['expiration'], p['dte'],
                            p['short'], p['long'], p['width'], p['opt'],
                            round(p['short_delta'], 4), round(p['credit'], 3),
                            round(p['raw_mid'], 3), round(p['slip_leg'], 3),
                            p['qty'], p['spot'], round(p['ivr'], 1),
                            round(p['iv_used'], 4), p['regime'], p['leadership'],
                            p['exit_bar']['date'], p['exit_bar']['time_et'],
                            round(p['exit_cost'], 3), p['exit_reason'],
                            round(p['pnl'], 2)])


class CacheOnlyPolygon(eng.PolygonClient):
    """Disk-cache-only Polygon client for logic validation: never hits the
    API and never writes the cache (avoids poisoning the overlay with [])."""
    def get_option_bars(self, occ_symbol, start_date, end_date, multiplier=5, timespan='minute'):
        key = f"polygon:bars_{occ_symbol}_{start_date}_{end_date}_{multiplier}_{timespan}"
        res = self.disk_cache.get(key)
        return res if res is not None else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2025-03-03')
    ap.add_argument('--end', default='2026-02-27')
    ap.add_argument('--stop-mult', type=float, default=1.0)
    ap.add_argument('--tp-pct', type=float, default=0.50)
    ap.add_argument('--delta-cap', type=float, default=0.30)
    ap.add_argument('--no-regime-gate', action='store_true')
    ap.add_argument('--no-iv-filter', action='store_true')
    ap.add_argument('--no-stop', action='store_true')
    ap.add_argument('--tag', default='run')
    args = ap.parse_args()

    # neutralize engine-level ticker exclusions for the proposed universe
    eng.TICKER_BLACKLIST.clear()

    cfg = {
        'start': args.start, 'end': args.end,
        'stop_mult': None if args.no_stop else args.stop_mult,
        'tp_pct': args.tp_pct, 'delta_cap': args.delta_cap,
        'use_regime_gate': not args.no_regime_gate,
        'use_iv_filter': not args.no_iv_filter,
    }
    print(f"[{args.tag}] cfg={cfg}", flush=True)

    alerts_by_date = load_alerts(FLOW_CSV)
    regime = load_regime(os.path.join(HERE, 'regime_series.csv'))
    closes = load_underlying(os.path.join(HERE, 'underlying_daily_closes.csv'))
    iv_daily = build_iv_daily(alerts_by_date)
    slip_model = build_slippage_model(alerts_by_date)
    print(f"slippage model (half-spread/leg): "
          f"{ {k: round(v,3) for k,v in sorted(slip_model.items())} }", flush=True)

    if os.environ.get('CACHE_ONLY') == '1':
        polygon = CacheOnlyPolygon(eng.POLYGON_API_KEY)
        print('!! CACHE_ONLY mode: no Polygon API calls', flush=True)
    else:
        polygon = eng.PolygonClient(eng.POLYGON_API_KEY)
    tradier = eng.TradierClient(eng.TRADIER_API_KEY)

    sim = ProposedSim(polygon, tradier, alerts_by_date, regime, iv_daily,
                      slip_model, closes, cfg)
    summary = sim.run()
    sim.dump_trades(os.path.join(HERE, f'trades_{args.tag}.csv'))
    with open(os.path.join(HERE, f'summary_{args.tag}.json'), 'w') as f:
        json.dump({'cfg': {k: v for k, v in cfg.items()}, 'summary': summary}, f, indent=2)
    print(json.dumps(summary, indent=2))
    dc = eng.get_disk_cache()
    print('cache:', dc.stats(), '| polygon api calls:', polygon.api_calls, flush=True)


if __name__ == '__main__':
    main()
