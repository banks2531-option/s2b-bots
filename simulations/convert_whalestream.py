#!/usr/bin/env python3
"""Convert whalestream daily JSON alert archives to engine-compatible CSV.

Runs ON THE DROPLET via `ssh root@host python3 - < this_file > out.csv`.
Reads /root/trading-bot/data/whalestream_options/options_alerts_*.json one
file at a time (2GB RAM box), filters to the replay engine's effective
universe + premium floor, and streams a whalestream-schema CSV to stdout
with extra columns (Delta, IV, ExpiryDate, BidAskSpread, ExpiresInDays)
appended for the proposed-rules simulator.

Filter is engine-equivalent: SignalAggregator.add_alert drops anything with
premium < 50_000 or effective_symbol not in WATCHLIST, so dropping those
rows here changes nothing about engine behavior.
"""
import csv
import glob
import json
import sys

WATCHLIST = {
    'SPY', 'QQQ', 'IWM',
    'AAPL', 'MSFT', 'AMZN', 'GOOGL', 'GOOG', 'META', 'NVDA', 'TSLA',
    'AMD', 'NFLX', 'CRM', 'AVGO', 'ORCL',
    'XLF', 'XLE', 'XLK', 'XLV', 'XLI', 'XLP', 'XLU', 'XLY', 'XLB',
    'GLD', 'SLV', 'TLT', 'HYG', 'EEM', 'EWZ', 'FXI',
    'SOXX', 'SMH', 'ARKK', 'DIA', 'VXX', 'UVXY',
    'JPM', 'BAC', 'GS', 'MS', 'WFC', 'C', 'V', 'MA',
    'BA', 'CAT', 'UNH', 'JNJ', 'PFE', 'ABBV', 'LLY',
    'COIN', 'PLTR', 'SQ', 'SHOP', 'SNOW', 'UBER', 'ABNB',
    'DIS', 'COST', 'WMT', 'HD', 'MCD', 'SBUX', 'NKE',
    'XOM', 'CVX', 'OXY', 'SLB',
    'BABA', 'PDD', 'NIO', 'RIVN', 'LCID',
    # index symbols the engine maps to ETFs
    'SPX', 'SPXW', 'NDX', 'RUT',
}
MIN_PREMIUM = 50000

COLS = ['Date', 'Symbol', 'Type', 'Premium', 'Spot Price', 'Strike Price',
        'Price', 'Bid', 'Ask', 'Contracts', 'Open Interest', 'Order Type',
        'Expiring', 'Golden Sweep', 'Highly Unusual', 'Unusual',
        'Aggressive', 'Sales Flow', 'Bid/Ask',
        'Delta', 'IV', 'ExpiryDate', 'BidAskSpread', 'ExpiresInDays']

def tf(x):
    return 'TRUE' if x else 'FALSE'

def main():
    files = sorted(glob.glob('/root/trading-bot/data/whalestream_options/options_alerts_*.json'))
    w = csv.writer(sys.stdout)
    w.writerow(COLS)
    n_in = n_out = 0
    for fp in files:
        try:
            with open(fp) as f:
                alerts = json.load(f)
        except Exception as e:
            sys.stderr.write('SKIP %s: %s\n' % (fp, e))
            continue
        for a in alerts:
            n_in += 1
            try:
                sym = (a.get('symbol') or a.get('asset', {}).get('ticker') or '').upper()
                if sym not in WATCHLIST:
                    continue
                prem = float(a.get('premium') or 0)
                if prem < MIN_PREMIUM:
                    continue
                # '2025-03-04T21:48:53.000000Z' (UTC) -> '2025-03-04 21:48:53'
                d = (a.get('date') or '').replace('T', ' ')[:19]
                if not d:
                    continue
                opt_type = 'CALL' if a.get('is_call') else 'PUT'
                spec = (a.get('formatted_specific_type') or '').lower()
                order_type = 'Sweep' if 'sweep' in spec else 'Block'
                exp = (a.get('expires_at') or '')[:10]
                w.writerow([
                    d, sym, opt_type, prem,
                    a.get('spot_price') or 0, a.get('strike_price') or 0,
                    a.get('price') or 0, a.get('bid') or 0, a.get('ask') or 0,
                    a.get('size') or 0, a.get('open_interest') or 0,
                    order_type, '',
                    tf(a.get('is_golden')), tf(a.get('is_highly_unusual')),
                    tf(a.get('is_unusual')), tf(a.get('is_aggressive')),
                    tf(a.get('is_sales_flow')), a.get('bid_ask_indicator') or '',
                    a.get('delta') if a.get('delta') is not None else '',
                    a.get('iv') if a.get('iv') is not None else '',
                    exp, a.get('bid_ask_spread') or '',
                    a.get('expires_in_days') if a.get('expires_in_days') is not None else '',
                ])
                n_out += 1
            except Exception:
                continue
        del alerts
        sys.stderr.write('%s done (%d kept so far)\n' % (fp.split('/')[-1], n_out))
    sys.stderr.write('TOTAL in=%d out=%d\n' % (n_in, n_out))

if __name__ == '__main__':
    main()
