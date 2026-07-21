#!/usr/bin/env python3
"""
REPLAY ENGINE v3.4.5 — CALIBRATED SLIPPAGE (iVolatility + Polygon)
===================================================================
Based on v3.4. CRITICAL FIX: Pricing methodology mismatch.

v3.4.0 BUG:
  Used iVol (short_bid - long_ask) as entry credit, replacing Polygon mid.
  Individual leg quotes from different 5-min snapshots don't combine into
  valid spread prices — produced fantasy credits up to 4x Polygon mid.
  This caused 31 extra trades and -64.8% return (vs +179.6% baseline).

v3.4.1 FIX:
  iVol now measures HALF-SPREAD WIDTH per leg only, not replacement prices.
  Polygon close remains the pricing backbone for all spread valuations.
  
  Entry: credit = polygon_mid - (ivol_half_spread_per_leg × 2)
  Exit:  cost   = polygon_spread + (ivol_half_spread_per_leg × 2)
  
  This gives realistic execution costs from real NBBO data without
  creating a parallel pricing universe that diverges from Polygon.

EXECUTION MODELS:
  fixed    — Polygon close ± $0.05/leg fixed slippage (v3.3.2 behavior)
  calibrated — Polygon close ± iVol-measured half-spread per leg (v3.4.1)

CLI:
  python replay_engine_v341.py -d data.csv                              # Fixed (v3.3.2)
  python replay_engine_v341.py -d data.csv --execution-model calibrated # iVol calibrated
  python replay_engine_v341.py -d data.csv --execution-model calibrated --commissions
"""

import csv
import os
import sys
import json
import time
import math
import argparse
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import List, Dict, Tuple, Optional
import urllib.request
import urllib.error

from disk_cache import get_disk_cache

# ============================================================
# CONFIGURATION — Mirrors v16.5 exactly
# ============================================================

TRADIER_API_KEY = os.environ.get("TRADIER_API_KEY", "")
TRADIER_BASE_URL = "https://sandbox.tradier.com/v1"

# v3.3: Polygon/Massive API
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.massive.com"

# Signal detection (v16.5)
MIN_PREMIUM = 50000
WHALE_PREMIUM = 200000
MIN_ACTIONABLE_PREMIUM = 500000
MIN_CONVICTION = 0.90
MIN_ALERTS = 1
SIGNAL_TTL_MINUTES = 30
MAX_SIGNAL_AGE_MINUTES = 15

# Quality weights (v16.5)
QUALITY_WEIGHTS = {
    'golden_sweep': 5.0,
    'highly_unusual': 3.0,
    'unusual': 2.0,
    'aggressive': 1.5,
    'sweep': 1.2,
    'block': 1.0,
}

# Flow coherence (v16.5)
COHERENCE_THRESHOLD = 0.52
COHERENCE_OVERRIDE = 0.95

# Position management
STARTING_CAPITAL = 25000
MAX_POSITIONS = 5
MAX_POSITION_SIZE_PCT = 0.20
MAX_CONTRACTS = 10
TAKE_PROFIT_PCT = 0.50
# v3.4.4: Trailing stop config
TRAIL_ACTIVATION_PCT = 0.50
TRAIL_RETRACE_PCT = 0.20
MIN_DTE = 3
MAX_DTE = 9
MIN_CREDIT_RATIO = 0.12
MAX_CREDIT_RATIO = 1.0     # v3.4.5: Cap credit/width ratio (1.0 = disabled, 0.35 = aggressive filter)

# Slippage defaults
DEFAULT_SLIPPAGE_PER_LEG = 0.05

# Rolling config (V16.6.1 match)
ROLL_TRIGGER_MULTIPLIER = 2.0   # Roll when spread >= 2.0x credit
ROLL_MAX_DTE = 3                # Only roll when DTE <= 3
ROLL_MAX_PER_POSITION = 2       # Max rolls per position
ROLL_NEW_MIN_DTE = 7            # New expiry must be at least 7 days out
ROLL_NEW_MAX_DTE = 14           # New expiry max 14 days out
ROLL_ENABLED_DEFAULT = True     # Can be disabled with --no-rolling

# v3.4.1: iVolatility config
IVOL_API_KEY = os.environ.get('IVOL_API_KEY', '')
IVOL_BASE_URL = "https://restapi.ivolatility.com"
IVOL_RATE_LIMIT = 1.1           # seconds between requests

# v3.4.1: Commission config
COMMISSION_PER_LEG = 0.65       # per contract per leg
COMMISSION_ENABLED_DEFAULT = False

# Credit stop config
CREDIT_STOP_MULTIPLIER = 0.0    # 0 = disabled; 2.5 = stop at 2.5x credit

# Market bias BPS blocking threshold (0 = disabled; 0.65 = block BPS when market >65% bearish)
MARKET_BIAS_BLOCK_THRESHOLD = 0.65

# VIX hysteresis for BPS blocking (0 = disabled)
VIX_BLOCK_BPS = 0       # Block BPS entries above this VIX level
VIX_UNBLOCK_BPS = 0     # Re-enable BPS below this VIX level

# BCS preference in bearish markets (0 = disabled; 0.60 = force BCS when market >60% bearish)
BCS_PREFERENCE_THRESHOLD = 0.0

# Scan interval (minutes)
DEFAULT_SCAN_INTERVAL = 5

# Default bar size for Polygon
DEFAULT_BAR_SIZE_MIN = 5

# Entry cutoff: no new trades after 3:30 PM ET
ENTRY_CUTOFF_HOUR_UTC = 20
ENTRY_CUTOFF_MINUTE_UTC = 30

# Ticker filters
TICKER_BLACKLIST = {"BABA", "AMD", "JPM", "NFLX", "GOOG", "MSFT", "TSLA"}
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
}

INDEX_TO_ETF = {
    'SPX': ('SPY', lambda p: p / 10),
    'NDX': ('QQQ', lambda p: p / 40),
    'RUT': ('IWM', lambda p: p / 10),
}

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('replay')

# ============================================================
# POLYGON/MASSIVE API CLIENT (v3.3 — 5-min option bars)
# ============================================================

class PolygonClient:
    """Fetches intraday option bars from Polygon/Massive API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.cache = {}
        self.disk_cache = get_disk_cache()
        self.api_calls = 0
        self.last_call_time = 0
        self.min_interval = 0.15  # ~7 calls/sec to stay safe

    def _rate_limit(self):
        elapsed = time.time() - self.last_call_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call_time = time.time()

    def _request(self, url):
        """Make a GET request to Polygon API."""
        self._rate_limit()
        # Append API key if not already present
        separator = '&' if '?' in url else '?'
        full_url = f"{url}{separator}apiKey={self.api_key}"
        req = urllib.request.Request(full_url)
        req.add_header('Accept', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.api_calls += 1
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logger.warning("   ⚠️ Polygon rate limited, waiting 12s...")
                time.sleep(12)
                return self._request(url)
            if e.code == 403:
                logger.error(f"   ❌ Polygon 403 Forbidden — check API key/plan")
                return None
            logger.error(f"   ❌ Polygon API error {e.code}: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"   ❌ Polygon request failed: {e}")
            return None

    def get_option_bars(self, occ_symbol, start_date, end_date, multiplier=5, timespan='minute'):
        """
        Fetch aggregated OHLCV bars for an option contract.
        Returns list of bars: [{t, o, h, l, c, v, vw, n}, ...]

        occ_symbol: Standard OCC format (e.g. AVGO251205C00400000)
        Polygon requires O: prefix (e.g. O:AVGO251205C00400000)
        """
        cache_key = f"bars_{occ_symbol}_{start_date}_{end_date}_{multiplier}_{timespan}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Check disk cache before hitting API
        disk_result = self.disk_cache.get(f"polygon:{cache_key}")
        if disk_result is not None:
            self.cache[cache_key] = disk_result
            return disk_result

        # Try composing from per-day cache entries (Databento format)
        composed = self._compose_from_daily_keys(occ_symbol, start_date, end_date, multiplier, timespan)
        if composed is not None:
            self.cache[cache_key] = composed
            return composed

        # Convert OCC to Polygon ticker format
        poly_ticker = f"O:{occ_symbol}"

        all_bars = []
        url = (f"{POLYGON_BASE_URL}/v2/aggs/ticker/{poly_ticker}"
               f"/range/{multiplier}/{timespan}/{start_date}/{end_date}"
               f"?adjusted=true&sort=asc&limit=50000")

        while url:
            data = self._request(url)
            if not data:
                break

            results = data.get('results', [])
            if results:
                all_bars.extend(results)

            # Handle pagination
            next_url = data.get('next_url')
            if next_url and len(results) > 0:
                url = next_url
            else:
                url = None

        self.cache[cache_key] = all_bars
        # Persist to disk for future runs
        self.disk_cache.put(f"polygon:{cache_key}", all_bars)
        return all_bars

    def _compose_from_daily_keys(self, occ_symbol, start_date, end_date, multiplier, timespan):
        """Compose multi-day bars from per-day cache entries (Databento format).

        Databento stores bars as polygon:bars_{OCC}_{date}_{date}_{mult}_{timespan}
        while v345 expects polygon:bars_{OCC}_{start}_{end}_{mult}_{timespan}.
        This iterates each trading day and concatenates cached per-day bars.
        """
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        all_bars = []
        found_any = False
        current = start_dt
        while current <= end_dt:
            if current.weekday() < 5:  # Mon-Fri
                ds = current.strftime('%Y-%m-%d')
                if ds not in US_MARKET_HOLIDAYS:
                    day_key = f"polygon:bars_{occ_symbol}_{ds}_{ds}_{multiplier}_{timespan}"
                    day_bars = self.disk_cache.get(day_key)
                    if day_bars is not None and len(day_bars) > 0:
                        all_bars.extend(day_bars)
                        found_any = True
            current += timedelta(days=1)
        if not found_any:
            return None
        # Sort by timestamp
        all_bars.sort(key=lambda b: b.get('t', 0))
        return all_bars

    def get_daily_bars(self, occ_symbol, start_date, end_date):
        """Fetch daily OHLCV bars as fallback."""
        return self.get_option_bars(occ_symbol, start_date, end_date,
                                     multiplier=1, timespan='day')


# ============================================================
# TRADIER API CLIENT (kept for strikes lookup only)
# ============================================================

class TradierClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.cache = {}
        self.disk_cache = get_disk_cache()
        self.api_calls = 0
        self.last_call_time = 0
        self.min_interval = 0.35

    def _rate_limit(self):
        elapsed = time.time() - self.last_call_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_call_time = time.time()

    def _request(self, endpoint, params=None):
        self._rate_limit()
        url = f"{TRADIER_BASE_URL}/{endpoint}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {self.api_key}')
        req.add_header('Accept', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.api_calls += 1
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                logger.warning("   ⚠️ Rate limited, waiting 5s...")
                time.sleep(5)
                return self._request(endpoint, params)
            logger.error(f"   ❌ Tradier API error {e.code}: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"   ❌ Tradier request failed: {e}")
            return None

    def get_strikes(self, symbol, expiration):
        cache_key = f"strikes_{symbol}_{expiration}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Check disk cache
        disk_result = self.disk_cache.get(f"tradier:{cache_key}")
        if disk_result is not None:
            self.cache[cache_key] = disk_result
            return disk_result

        data = self._request('markets/options/strikes', {
            'symbol': symbol, 'expiration': expiration,
        })
        if not data or 'strikes' not in data or not data['strikes']:
            self.cache[cache_key] = []
            self.disk_cache.put(f"tradier:{cache_key}", [])
            return []
        strikes = data['strikes'].get('strike', [])
        if isinstance(strikes, (int, float)):
            strikes = [strikes]
        self.cache[cache_key] = [float(s) for s in strikes]
        self.disk_cache.put(f"tradier:{cache_key}", self.cache[cache_key])
        return self.cache[cache_key]



# ============================================================
# iVOLATILITY INTRADAY CLIENT (v3.4)
# ============================================================

class IVolClient:
    """
    Queries iVolatility intraday API for real NBBO bid/ask quotes.
    Endpoint: /equities/intraday/single-equity-option-rawiv
    Returns 5-min bars with: optionBidPrice, optionAskPrice, optionIv, delta, etc.
    """
    def __init__(self, api_key=IVOL_API_KEY):
        self.api_key = api_key
        self.requests_made = 0
        self._last = 0
        self._cache = {}  # (symbol, date, strike, expDate, optType) → bars
        self.disk_cache = get_disk_cache()

    def _throttle(self):
        elapsed = time.time() - self._last
        if elapsed < IVOL_RATE_LIMIT:
            time.sleep(IVOL_RATE_LIMIT - elapsed)
        self._last = time.time()
        self.requests_made += 1

    def get_bars(self, symbol, date, exp_date, strike, opt_type):
        """Get 5-min intraday bars with bid/ask for a specific option on a specific date."""
        cache_key = (symbol, date, str(strike), exp_date, opt_type)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Check disk cache before hitting API
        disk_key = f"ivol:{symbol}:{date}:{strike}:{exp_date}:{opt_type}"
        disk_result = self.disk_cache.get(disk_key)
        if disk_result is not None:
            self._cache[cache_key] = disk_result
            return disk_result

        self._throttle()
        params = (f"apiKey={self.api_key}&symbol={symbol}&date={date}"
                  f"&expDate={exp_date}&strike={strike}&optType={opt_type}"
                  f"&minuteType=MINUTE_5")
        url = f"{IVOL_BASE_URL}/equities/intraday/single-equity-option-rawiv?{params}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
                bars = body.get('data', [])
                self._cache[cache_key] = bars
                self.disk_cache.put(disk_key, bars)
                return bars
        except Exception as e:
            logger.debug(f"iVol API error: {e}")
            self._cache[cache_key] = []
            self.disk_cache.put(disk_key, [])
            return []

    def get_quote_at_time(self, symbol, date, exp_date, strike, opt_type, target_time_hhmm):
        """
        Get the bid/ask quote closest to target_time (HH:MM ET format).
        Returns dict with optionBidPrice, optionAskPrice, etc. or None.
        """
        bars = self.get_bars(symbol, date, exp_date, strike, opt_type)
        if not bars:
            return None

        try:
            th, tm = int(target_time_hhmm.split(':')[0]), int(target_time_hhmm.split(':')[1])
            target_min = th * 60 + tm
        except:
            target_min = 15 * 60 + 55  # Default EOD

        best_bar = None
        best_diff = float('inf')
        for bar in bars:
            ts = bar.get('timestamp', '')
            try:
                tp = ts.split(' ')[1] if ' ' in ts else ts
                h, m = int(tp.split(':')[0]), int(tp.split(':')[1])
                bm = h * 60 + m
            except:
                continue
            diff = abs(bm - target_min)
            if bm <= target_min:
                diff -= 0.5  # Prefer bar at or just before target
            if diff < best_diff:
                best_diff = diff
                best_bar = bar

        return best_bar

    def get_spread_quote(self, symbol, date, exp_date, short_strike, long_strike,
                         opt_type, target_time_hhmm):
        """
        Get bid/ask for both legs of a credit spread at a specific time.
        Returns dict with entry/exit pricing or None.
        
        Credit spread entry: sell short @ bid, buy long @ ask
        Credit spread exit:  buy short @ ask, sell long @ bid
        """
        short_bar = self.get_quote_at_time(
            symbol, date, exp_date, short_strike, opt_type, target_time_hhmm)
        long_bar = self.get_quote_at_time(
            symbol, date, exp_date, long_strike, opt_type, target_time_hhmm)

        if not short_bar or not long_bar:
            return None

        sb = float(short_bar.get('optionBidPrice', 0) or 0)
        sa = float(short_bar.get('optionAskPrice', 0) or 0)
        lb = float(long_bar.get('optionBidPrice', 0) or 0)
        la = float(long_bar.get('optionAskPrice', 0) or 0)

        if sb == 0 and sa == 0:
            return None

        # Entry credit = short_bid - long_ask (we sell short, buy long)
        entry_credit = sb - la
        # Exit debit = short_ask - long_bid (we buy short, sell long)
        exit_debit = sa - lb
        # Mid-price spread = average of entry and exit
        mid_spread = (entry_credit + exit_debit) / 2

        return {
            'short_bid': sb, 'short_ask': sa,
            'long_bid': lb, 'long_ask': la,
            'entry_credit': entry_credit,  # What we collect selling the spread
            'exit_debit': exit_debit,      # What we pay closing the spread
            'mid_spread': mid_spread,
            'short_iv': float(short_bar.get('optionIv', 0) or 0),
            'long_iv': float(long_bar.get('optionIv', 0) or 0),
        }


# ============================================================
# HELPERS
# ============================================================

def build_occ_symbol(symbol, expiration, option_type, strike):
    if '-' in expiration:
        exp_dt = datetime.strptime(expiration, '%Y-%m-%d')
    else:
        exp_dt = datetime.strptime(expiration, '%m/%d/%Y')
    date_str = exp_dt.strftime('%y%m%d')
    type_char = 'C' if option_type.upper() in ('CALL', 'C') else 'P'
    strike_str = f"{int(strike * 1000):08d}"
    return f"{symbol}{date_str}{type_char}{strike_str}"


def find_nearest_strike(target, strikes):
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - target))


def get_weekly_expiration(signal_date, min_dte=3, max_dte=14):
    candidate = signal_date + timedelta(days=min_dte)
    while candidate.weekday() != 4:
        candidate += timedelta(days=1)
    dte = (candidate - signal_date).days
    if dte > max_dte:
        return None
    return candidate.strftime('%Y-%m-%d')


# v3.4.6: US market holidays — split into clusters vs single-day
# Cluster holidays: multiple closures within ~8 trading days, skeleton crew liquidity,
# compressed theta, positions get trapped across multi-day gaps.
# Single-day holidays: one day off, normal liquidity before/after, rolls still viable.
#
# Backtest evidence (56 days, Dec 2025 – Feb 2026):
#   - Blanket blackout fixed Christmas/NYE (-$8,327 saved) but broke Presidents' Day
#     by blocking 4 viable rolls on Feb 13 → all expired max loss → -$13,110
#   - Cluster-only blackout preserves the Christmas fix while allowing single-day rolls

HOLIDAY_CLUSTERS = {
    # Thanksgiving week: Thu closed, Fri half-day/low liquidity, positions trapped Thu-Mon
    '2025-11-27',  # Thanksgiving 2025
    '2025-12-25',  # Christmas 2025
    '2026-01-01',  # New Year's Day 2026
    '2026-11-26',  # Thanksgiving 2026
    '2026-12-25',  # Christmas 2026
    '2027-01-01',  # New Year's Day 2027
}

SINGLE_DAY_HOLIDAYS = {
    # 2025 holidays (for full-year backtest starting March 2025)
    '2025-04-18',  # Good Friday 2025
    '2025-05-26',  # Memorial Day 2025
    '2025-06-19',  # Juneteenth 2025
    '2025-07-04',  # Independence Day 2025
    '2025-09-01',  # Labor Day 2025
    # 2026 holidays
    '2026-01-19',  # MLK Day
    '2026-02-16',  # Presidents Day
    '2026-04-03',  # Good Friday
    '2026-05-25',  # Memorial Day
    '2026-07-03',  # Independence Day (observed)
    '2026-09-07',  # Labor Day
}

# Combined set for reference
US_MARKET_HOLIDAYS = HOLIDAY_CLUSTERS | SINGLE_DAY_HOLIDAYS


def has_holiday_in_range(start_date_str, end_date_str, clusters_only=False):
    """Check if a market holiday falls between start and end dates (inclusive).

    clusters_only=True: only flag Thanksgiving/Christmas/NYE clusters
    clusters_only=False: flag any holiday (original blanket behavior)
    """
    holidays = HOLIDAY_CLUSTERS if clusters_only else US_MARKET_HOLIDAYS
    start = datetime.strptime(start_date_str, '%Y-%m-%d').date() if isinstance(start_date_str, str) else start_date_str
    end = datetime.strptime(end_date_str, '%Y-%m-%d').date() if isinstance(end_date_str, str) else end_date_str
    current = start
    while current <= end:
        date_key = current.strftime('%Y-%m-%d')
        if date_key in holidays:
            return True, date_key
        current += timedelta(days=1)
    return False, None


def ts_to_date(unix_ms):
    """Convert Unix millisecond timestamp to YYYY-MM-DD date string in US/Eastern."""
    # Polygon timestamps are in ms, represent start of the bar in ET
    utc_dt = datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc)
    # Rough ET conversion: UTC-5 (EST) or UTC-4 (EDT)
    # For Dec 2025, it's EST (UTC-5). For simplicity, subtract 5 hours.
    # Market hours are 9:30-16:00 ET, so bars between 14:30-21:00 UTC
    et_dt = utc_dt - timedelta(hours=5)
    return et_dt.strftime('%Y-%m-%d')


def ts_to_time(unix_ms):
    """Convert Unix ms timestamp to HH:MM ET string."""
    utc_dt = datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc)
    et_dt = utc_dt - timedelta(hours=5)
    return et_dt.strftime('%H:%M')


# ============================================================
# FLOW ALERT PARSER
# ============================================================

class FlowAlert:
    def __init__(self, row):
        self.timestamp = self._parse_date(row['Date'])
        self.symbol = row['Symbol'].strip()
        self.option_type = row['Type'].strip().upper()
        self.premium = self._parse_money(row['Premium'])
        self.spot_price = self._parse_money(row['Spot Price'])
        self.strike = self._parse_money(row['Strike Price'])
        self.price = self._parse_money(row['Price'])
        self.bid = self._parse_float(row.get('Bid', '0'))
        self.ask = self._parse_float(row.get('Ask', '0'))
        self.contracts = self._parse_int(row.get('Contracts', '0'))
        self.open_interest = self._parse_int(row.get('Open Interest', '0'))
        self.order_type = row.get('Order Type', '').strip()
        self.expiring = row.get('Expiring', '').strip()
        self.is_golden_sweep = row.get('Golden Sweep', 'FALSE').strip().upper() == 'TRUE'
        self.is_highly_unusual = row.get('Highly Unusual', 'FALSE').strip().upper() == 'TRUE'
        self.is_unusual = row.get('Unusual', 'FALSE').strip().upper() == 'TRUE'
        self.is_aggressive = row.get('Aggressive', 'FALSE').strip().upper() == 'TRUE'
        self.is_sales_flow = row.get('Sales Flow', 'FALSE').strip().upper() == 'TRUE'
        self.bid_ask_side = row.get('Bid/Ask', '').strip()
        self.sentiment = self._determine_sentiment()
        self.quality_weight = self._determine_quality_weight()

    def _parse_date(self, date_str):
        date_str = date_str.strip()
        for fmt in ['%m/%d/%Y %H:%M', '%m/%d/%y %H:%M', '%m/%d/%Y %H:%M:%S',
                     '%m/%d/%y %I:%M:%S %p', '%Y-%m-%d %H:%M:%S']:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        try:
            return datetime.strptime(date_str.split(' ')[0], '%m/%d/%Y')
        except:
            return datetime.strptime(date_str.split(' ')[0], '%m/%d/%y')

    def _parse_money(self, val):
        if not val: return 0.0
        return float(str(val).replace('$', '').replace(',', '').replace(' ', ''))

    def _parse_float(self, val):
        try: return float(str(val).replace('$', '').replace(',', '').replace(' ', ''))
        except: return 0.0

    def _parse_int(self, val):
        try: return int(str(val).replace(',', '').replace(' ', ''))
        except: return 0

    def _determine_sentiment(self):
        is_call = self.option_type == 'CALL'
        if self.is_sales_flow:
            return -1 if is_call else 1
        else:
            return 1 if is_call else -1

    def _determine_quality_weight(self):
        if self.is_golden_sweep: return QUALITY_WEIGHTS['golden_sweep']
        if self.is_highly_unusual: return QUALITY_WEIGHTS['highly_unusual']
        if self.is_unusual: return QUALITY_WEIGHTS['unusual']
        if self.is_aggressive: return QUALITY_WEIGHTS['aggressive']
        if self.order_type.lower() == 'sweep': return QUALITY_WEIGHTS['sweep']
        return QUALITY_WEIGHTS['block']

    @property
    def date_key(self):
        return self.timestamp.strftime('%Y-%m-%d')

    @property
    def effective_symbol(self):
        if self.symbol in INDEX_TO_ETF: return INDEX_TO_ETF[self.symbol][0]
        return self.symbol

    @property
    def effective_spot(self):
        if self.symbol in INDEX_TO_ETF:
            _, converter = INDEX_TO_ETF[self.symbol]
            return converter(self.spot_price)
        return self.spot_price


# ============================================================
# SIGNAL AGGREGATOR — Rolling window, no future peeking
# ============================================================

class SignalAggregator:
    def __init__(self):
        self.all_alerts = []

    def add_alert(self, alert):
        sym = alert.effective_symbol
        if alert.premium < MIN_PREMIUM:
            return
        if sym not in WATCHLIST:
            return
        self.all_alerts.append(alert)

    def get_actionable(self, current_time):
        window_start = current_time - timedelta(minutes=SIGNAL_TTL_MINUTES)
        by_symbol = defaultdict(list)
        for a in self.all_alerts:
            if a.timestamp > current_time:
                continue
            if a.timestamp < window_start:
                continue
            by_symbol[a.effective_symbol].append(a)

        profiles = {}
        for symbol, alerts in by_symbol.items():
            bullish_premium = 0
            bearish_premium = 0
            bullish_count = 0
            bearish_count = 0
            freshest = None
            latest_spot = 0

            for alert in alerts:
                prem = alert.premium
                weight = alert.quality_weight
                age_minutes = (current_time - alert.timestamp).total_seconds() / 60
                decay = max(0.2, 1.0 - (age_minutes / 30))
                effective_premium = prem * weight * decay

                if alert.sentiment > 0:
                    bullish_premium += effective_premium
                    bullish_count += 1
                elif alert.sentiment < 0:
                    bearish_premium += effective_premium
                    bearish_count += 1

                if freshest is None or alert.timestamp > freshest:
                    freshest = alert.timestamp
                spot = alert.effective_spot
                if spot > 0:
                    latest_spot = spot

            total_prem = bullish_premium + bearish_premium
            if total_prem == 0:
                continue

            direction = 'bullish' if bullish_premium > bearish_premium else 'bearish'
            dominant_prem = max(bullish_premium, bearish_premium)
            conviction = dominant_prem / total_prem

            profiles[symbol] = {
                'bullish_premium': bullish_premium,
                'bearish_premium': bearish_premium,
                'bullish_count': bullish_count,
                'bearish_count': bearish_count,
                'direction': direction,
                'dominant_premium': dominant_prem,
                'conviction_ratio': conviction,
                'freshest': freshest,
                'spot_price': latest_spot,
            }

        actionable = []
        for symbol, profile in profiles.items():
            if profile['dominant_premium'] < MIN_ACTIONABLE_PREMIUM:
                continue
            dir_count = profile['bullish_count'] if profile['direction'] == 'bullish' else profile['bearish_count']
            if dir_count < MIN_ALERTS:
                continue
            if profile['conviction_ratio'] < MIN_CONVICTION:
                continue
            if profile['freshest']:
                age = (current_time - profile['freshest']).total_seconds()
                if age > MAX_SIGNAL_AGE_MINUTES * 60:
                    continue
            if symbol in TICKER_BLACKLIST:
                continue
            actionable.append((symbol, profile))

        actionable.sort(key=lambda x: x[1]['dominant_premium'], reverse=True)

        if len(actionable) >= 3:
            total_bull = sum(p['bullish_premium'] for _, p in actionable)
            total_bear = sum(p['bearish_premium'] for _, p in actionable)
            total = total_bull + total_bear
            if total > 0:
                bull_pct = total_bull / total
                bear_pct = total_bear / total
                if bear_pct > COHERENCE_THRESHOLD:
                    flow_bias = 'bearish'
                elif bull_pct > COHERENCE_THRESHOLD:
                    flow_bias = 'bullish'
                else:
                    flow_bias = 'neutral'
                if flow_bias != 'neutral':
                    filtered = []
                    for symbol, profile in actionable:
                        is_counter = (
                            (flow_bias == 'bearish' and profile['direction'] == 'bullish') or
                            (flow_bias == 'bullish' and profile['direction'] == 'bearish')
                        )
                        if is_counter and profile['conviction_ratio'] < COHERENCE_OVERRIDE:
                            continue
                        filtered.append((symbol, profile))
                    actionable = filtered

        return actionable[:10]

    def clear(self):
        self.all_alerts.clear()


# ============================================================
# POSITION
# ============================================================

class Position:
    def __init__(self, symbol, direction, entry_date, short_strike, long_strike,
                 expiration, short_occ, long_occ, credit, spread_width, quantity,
                 spot_at_entry, conviction, weighted_premium, signal_time, slippage_applied):
        self.symbol = symbol
        self.direction = direction
        self.entry_date = entry_date
        self.short_strike = short_strike
        self.long_strike = long_strike
        self.expiration = expiration
        self.short_occ = short_occ
        self.long_occ = long_occ
        self.credit = credit
        self.spread_width = spread_width
        self.quantity = quantity
        self.spot_at_entry = spot_at_entry
        self.conviction = conviction
        self.weighted_premium = weighted_premium
        self.signal_time = signal_time
        self.slippage_applied = slippage_applied

        self.max_profit = credit * quantity * 100
        self.max_loss = (spread_width - credit) * quantity * 100
        self.daily_values = {}       # date -> {close}  (daily summary from bars)
        self.intraday_bars = []      # v3.3: [{ts, date, spread, short_c, long_c}, ...]
        self.exit_date = None
        self.exit_reason = None
        self.pnl = 0
        self.status = 'open'
        self.tradier_data_found = False
        self.polygon_data_found = False
        self.polygon_bar_count = 0
        self.mae = 0
        self.mae_date = None
        self.tp_bar_time = None      # v3.3: Exact time TP was hit
        self.roll_count = 0          # v3.3.1: Number of times rolled
        self.total_credits = credit  # v3.3.1: Cumulative credits across rolls
        self.roll_history = []       # v3.3.1: [{date, old_exp, new_exp, close_cost, new_credit, net}]
        self.roll_cost_total = 0     # v3.3.2: Cumulative dollar cost of all rolls (applied to balance at roll time)
        self.lifecycle_pnl = 0       # v3.3.2: Full lifecycle P&L = final spread P&L + roll costs
        # v3.4.4: Trailing stop tracking
        self.trail_active = False
        self.trail_peak_profit_pct = 0
        self.trail_peak_spread = credit
        self.trail_activation_bar = None
        # v3.4.8: Profit recapture tracking
        self.was_profitable = False
        self.peak_profit_pct = 0
        # v3.4.9: Conditional iron condor tracking
        self.is_conditional_ic = False
        self.has_opposite_leg = False
        self.opposite_leg_credit = 0
        self.opposite_leg_short_strike = 0
        self.opposite_leg_long_strike = 0
        self.opposite_leg_short_occ = ''
        self.opposite_leg_long_occ = ''
        self.opposite_leg_bars = []
        self.opposite_leg_direction = None   # 'bullish' or 'bearish'
        self.original_direction = None       # 'bullish' (BPS) or 'bearish' (BCS)
        self.entry_spot = 0                  # Underlying price at entry
        self.total_credit = credit           # Combined credit (original + opposite)
        self.ic_trigger_date = None          # Date opposite leg was added
        self.ic_trigger_type = None          # What triggered the conversion

    @property
    def dte_at_entry(self):
        exp = datetime.strptime(self.expiration, '%Y-%m-%d')
        entry = datetime.strptime(self.entry_date, '%Y-%m-%d')
        return (exp - entry).days


# ============================================================
# REPLAY ENGINE v3.3
# ============================================================

class ReplayEngine:
    def __init__(self, tradier, polygon, capital=STARTING_CAPITAL,
                 slippage_per_leg=DEFAULT_SLIPPAGE_PER_LEG,
                 scan_interval_min=DEFAULT_SCAN_INTERVAL,
                 bar_size_min=DEFAULT_BAR_SIZE_MIN,
                 max_hold_days=0,
                 rolling_enabled=True,
                 credit_stop_multiplier=0.0,
                 execution_model='fixed',        # v3.4.1: 'fixed' or 'calibrated'
                 commissions_enabled=False,       # v3.4.1: $0.65/leg/contract
                 ivol_client=None,
                 tp_mode='fixed',
                 trail_activate_pct=0.50,
                 trail_retrace_pct=0.20,
                 stale_rolled_only=False,
                 max_credit_ratio=MAX_CREDIT_RATIO,
                 holiday_blackout=False,
                 max_positions=MAX_POSITIONS,
                 position_size_pct=MAX_POSITION_SIZE_PCT,
                 min_credit_ratio=MIN_CREDIT_RATIO,
                 roll_max_dte=ROLL_MAX_DTE,
                 market_bias_block=MARKET_BIAS_BLOCK_THRESHOLD,
                 vix_block_bps=VIX_BLOCK_BPS,
                 vix_unblock_bps=VIX_UNBLOCK_BPS,
                 bcs_preference_threshold=BCS_PREFERENCE_THRESHOLD,
                 take_profit_pct=TAKE_PROFIT_PCT,
                 put_only=False,
                 min_dte=MIN_DTE,
                 max_dte=MAX_DTE,
                 profit_recapture=False,
                 conditional_ic=False,
                 ic_trigger='flow_reversal',
                 ic_dte_mode='same_exp',
                 ic_price_move_pct=3.0,
                 ic_spread_sl_mult=1.5,
                 ic_dte_min=30,
                 ic_dte_max=45):
        self.tradier = tradier
        self.polygon = polygon
        self.starting_capital = capital
        self.balance = capital
        self.slippage_per_leg = slippage_per_leg
        self.scan_interval = scan_interval_min
        self.bar_size = bar_size_min
        self.max_hold_days = max_hold_days
        self.rolling_enabled = rolling_enabled
        self.credit_stop_multiplier = credit_stop_multiplier
        self.execution_model = execution_model     # v3.4
        self.commissions_enabled = commissions_enabled  # v3.4
        self.ivol = ivol_client                    # v3.4
        self.total_commissions = 0                 # v3.4.1: running total
        self.tp_mode = tp_mode
        self.trail_activate_pct = trail_activate_pct
        self.trail_retrace_pct = trail_retrace_pct
        self.stale_rolled_only = stale_rolled_only
        self.max_credit_ratio = max_credit_ratio
        # v3.4.6: holiday_blackout can be:
        #   False       — disabled
        #   'clusters'  — block only Thanksgiving/Christmas/NYE clusters (recommended)
        #   'all'       — block all holidays including single-day (original v3.4.5 behavior)
        self.holiday_blackout = holiday_blackout
        self.max_positions = max_positions
        self.position_size_pct = position_size_pct
        self.min_credit_ratio = min_credit_ratio
        self.roll_max_dte = roll_max_dte
        self.market_bias_block = market_bias_block
        self.vix_block_bps = vix_block_bps
        self.vix_unblock_bps = vix_unblock_bps
        self.bcs_preference_threshold = bcs_preference_threshold
        self.take_profit_pct = take_profit_pct
        self.put_only = put_only
        self.min_dte = min_dte
        self.max_dte = max_dte
        self.profit_recapture = profit_recapture
        # v3.4.9: Conditional iron condor
        self.conditional_ic = conditional_ic
        self.ic_trigger = ic_trigger
        self.ic_dte_mode = ic_dte_mode
        self.ic_price_move_pct = ic_price_move_pct
        self.ic_spread_sl_mult = ic_spread_sl_mult
        self.ic_dte_min = ic_dte_min
        self.ic_dte_max = ic_dte_max
        self.ic_stats = {'checked': 0, 'triggered': 0, 'built': 0, 'failed': 0}
        self.vix_bps_blocked = False  # VIX hysteresis state
        self.vix_daily = {}  # date_str -> VIX daily close (FRED fallback)
        self._vix_intraday = defaultdict(list)  # date_str -> [(min_utc, spot), ...] from flow
        self.positions = []
        self.closed_positions = []
        self.equity_curve = []
        self.all_signals = []
        self.trading_days = []
        self.rejected_signals = []
        self.stale_exits = 0
        self.stale_saved = 0
        self.roll_stats = {'attempted': 0, 'completed': 0, 'skipped': 0, 'total_net_credit': 0}
        self.stop_stats = {'triggered': 0, 'total_saved_vs_max': 0}
        self.ivol_stats = {'queries': 0, 'hits': 0, 'misses': 0}  # v3.4

    def _load_vix_data(self, csv_paths, start_date, end_date):
        """Load VIX data from two sources:
        1. Intraday: VIX spot prices from flow CSV (at trade time, sub-minute)
        2. Fallback: FRED daily closes for days with no flow data
        """
        # Phase 1: Extract intraday VIX from flow CSV
        from bisect import bisect_right
        self._vix_intraday = defaultdict(list)  # date_str -> [(minutes_since_midnight_utc, spot), ...]

        for csv_path in csv_paths:
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row['Symbol'].strip() != 'VIX':
                        continue
                    spot_str = row.get('Spot Price', '').replace('$', '').replace(',', '').strip()
                    try:
                        spot = float(spot_str)
                    except (ValueError, TypeError):
                        continue
                    if spot <= 0:
                        continue
                    date_str = row['Date'].strip()
                    # Parse ET timestamp
                    ts = None
                    for fmt in ['%m/%d/%y %I:%M:%S %p', '%m/%d/%Y %I:%M:%S %p',
                                '%m/%d/%y %H:%M', '%m/%d/%Y %H:%M']:
                        try:
                            ts = datetime.strptime(date_str, fmt)
                            break
                        except ValueError:
                            continue
                    if not ts:
                        continue
                    day = ts.strftime('%Y-%m-%d')
                    # Store as minutes since midnight UTC (ET + 5 for EST, +4 for EDT)
                    # Approximate: use +5 (close enough for regime gating)
                    min_utc = (ts.hour + 5) * 60 + ts.minute
                    self._vix_intraday[day].append((min_utc, spot))

        # Sort each day's observations by time
        intraday_days = 0
        for day in self._vix_intraday:
            self._vix_intraday[day].sort()
            intraday_days += 1

        # Phase 2: FRED daily closes as fallback
        dc = get_disk_cache()
        cache_key = f"fred_vix_{start_date}_{end_date}"
        cached = dc.get(cache_key)
        if cached:
            self.vix_daily.update(cached)
            logger.info(f"   📊 VIX: {intraday_days} days intraday from flow + "
                       f"{len(cached)} days daily close from FRED (cached)")
            return

        fred_key = os.environ.get('FRED_API_KEY', '')
        if not fred_key:
            logger.info(f"   📊 VIX: {intraday_days} days intraday from flow (no FRED key for fallback)")
            return

        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id=VIXCLS&api_key={fred_key}&file_type=json"
                   f"&observation_start={start_date}&observation_end={end_date}&sort_order=asc")
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            vix_data = {}
            for obs in data.get('observations', []):
                if obs['value'] != '.':
                    vix_data[obs['date']] = float(obs['value'])
            self.vix_daily.update(vix_data)
            dc.put(cache_key, vix_data)
            logger.info(f"   📊 VIX: {intraday_days} days intraday from flow + "
                       f"{len(vix_data)} days daily close from FRED")
        except Exception as e:
            logger.info(f"   📊 VIX: {intraday_days} days intraday from flow (FRED failed: {e})")

    def _get_vix_at_time(self, date_str, scan_time_utc_min):
        """Get VIX level nearest to a specific scan time on a given date.

        Args:
            date_str: 'YYYY-MM-DD'
            scan_time_utc_min: minutes since midnight UTC (e.g., 14*60+30 = 870 for 14:30 UTC)
        Returns:
            VIX value (float) or 0 if unavailable
        """
        from bisect import bisect_right

        # Try intraday first
        if date_str in self._vix_intraday:
            obs = self._vix_intraday[date_str]  # sorted [(min_utc, spot), ...]
            times = [t for t, _ in obs]
            idx = bisect_right(times, scan_time_utc_min)
            # Find nearest observation (before or after)
            candidates = []
            if idx > 0:
                candidates.append((abs(times[idx-1] - scan_time_utc_min), obs[idx-1][1]))
            if idx < len(obs):
                candidates.append((abs(times[idx] - scan_time_utc_min), obs[idx][1]))
            if candidates:
                gap_min, vix = min(candidates)
                if gap_min <= 30:  # Within 30 min
                    return vix

        # Fallback: FRED daily close
        if date_str in self.vix_daily:
            return self.vix_daily[date_str]

        # Carry forward
        prior = [d for d in sorted(self.vix_daily.keys()) if d < date_str]
        if prior:
            return self.vix_daily[prior[-1]]
        return 0

    def _apply_commission(self, num_legs, quantity):
        """v3.4.1: Calculate and debit commission from balance."""
        if not self.commissions_enabled:
            return 0
        commission = num_legs * quantity * COMMISSION_PER_LEG
        self.balance -= commission
        self.total_commissions += commission
        return commission

    def _ivol_calibrated_slippage(self, symbol, date_str, exp_date, short_strike, long_strike, opt_type, time_hhmm):
        """
        v3.4.1: Get real half-spread per leg from iVol bid/ask.
        Returns the AVERAGE half-spread across both legs, or None if unavailable.
        
        Half-spread = (ask - bid) / 2 per leg.
        This is the realistic execution cost: you lose half the spread on each leg.
        
        Does NOT return a replacement price — Polygon mid remains the price reference.
        """
        if not self.ivol or self.execution_model != 'calibrated':
            return None
        self.ivol_stats['queries'] += 1
        quote = self.ivol.get_spread_quote(
            symbol, date_str, exp_date, short_strike, long_strike, opt_type, time_hhmm)
        if not quote:
            self.ivol_stats['misses'] += 1
            return None
        
        sb, sa = quote['short_bid'], quote['short_ask']
        lb, la = quote['long_bid'], quote['long_ask']
        
        # Need valid quotes on both legs
        if sa <= 0 or la <= 0:
            self.ivol_stats['misses'] += 1
            return None
        
        short_half_spread = (sa - sb) / 2
        long_half_spread = (la - lb) / 2
        
        # Sanity check: half-spread should be non-negative and reasonable
        if short_half_spread < 0 or long_half_spread < 0:
            self.ivol_stats['misses'] += 1
            return None
        
        avg_half_spread = (short_half_spread + long_half_spread) / 2
        self.ivol_stats['hits'] += 1
        return avg_half_spread

    def load_flow_data(self, csv_path):
        logger.info(f"📂 Loading flow data from {csv_path}...")
        alerts_by_date = defaultdict(list)
        total = 0
        errors = 0
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    alert = FlowAlert(row)
                    alerts_by_date[alert.date_key].append(alert)
                    total += 1
                except Exception as e:
                    errors += 1
        dates = sorted(alerts_by_date.keys())
        logger.info(f"   ✅ Loaded {total:,} alerts across {len(dates)} trading days")
        logger.info(f"   📅 Date range: {dates[0]} to {dates[-1]}")
        if errors:
            logger.info(f"   ⚠️ {errors} parse errors skipped")
        return alerts_by_date

    def _find_spread(self, symbol, signal_date, spot, direction):
        target_exp = get_weekly_expiration(signal_date, self.min_dte, self.max_dte)
        if not target_exp:
            return None

        # v3.4.6: Holiday blackout — skip entries spanning major holiday clusters
        # Cluster-only by default: blocks Christmas/NYE/Thanksgiving but allows
        # single-day holidays (MLK, Presidents Day, etc.) where rolls remain viable
        if self.holiday_blackout:
            clusters_only = (self.holiday_blackout == 'clusters')
            has_holiday, holiday_date = has_holiday_in_range(
                signal_date.strftime('%Y-%m-%d'), target_exp, clusters_only=clusters_only)
            if has_holiday:
                mode_label = "cluster" if clusters_only else "any"
                logger.info(f"      ❌ Holiday blackout ({mode_label}): {holiday_date} falls between entry and expiry {target_exp}")
                return None

        strikes = self.tradier.get_strikes(symbol, target_exp)
        if not strikes or len(strikes) < 5:
            if spot < 50: inc = 1
            elif spot < 200: inc = 2.5
            elif spot < 500: inc = 5
            else: inc = 10
            base = round(spot / inc) * inc
            strikes = [base + i * inc for i in range(-30, 31)]

        target_width = max(3, min(10, spot * 0.01))

        if direction == 'bullish':
            short_target = spot * 0.97
            short_strike = find_nearest_strike(short_target, [s for s in strikes if s < spot])
            if not short_strike: return None
            long_target = short_strike - target_width
            long_strike = find_nearest_strike(long_target, [s for s in strikes if s < short_strike])
            if not long_strike: return None
            spread_width = short_strike - long_strike
            if spread_width <= 0: return None
            short_occ = build_occ_symbol(symbol, target_exp, 'P', short_strike)
            long_occ = build_occ_symbol(symbol, target_exp, 'P', long_strike)
        else:
            short_target = spot * 1.03
            short_strike = find_nearest_strike(short_target, [s for s in strikes if s > spot])
            if not short_strike: return None
            long_target = short_strike + target_width
            long_strike = find_nearest_strike(long_target, [s for s in strikes if s > short_strike])
            if not long_strike: return None
            spread_width = long_strike - short_strike
            if spread_width <= 0: return None
            short_occ = build_occ_symbol(symbol, target_exp, 'C', short_strike)
            long_occ = build_occ_symbol(symbol, target_exp, 'C', long_strike)

        return {
            'expiration': target_exp,
            'short_strike': short_strike,
            'long_strike': long_strike,
            'spread_width': spread_width,
            'short_occ': short_occ,
            'long_occ': long_occ,
            'direction': direction,
        }

    def _get_spread_price_history(self, short_occ, long_occ, entry_date, expiration):
        """
        v3.3: Fetches 5-min bars from Polygon for both legs.
        Returns tuple: (daily_values, intraday_bars, bar_count)

        daily_values: {date_str: {'close': float}} — last bar's spread each day
        intraday_bars: [{ts, date, time_et, spread_close}, ...] — every aligned bar
        bar_count: total aligned bars found
        """
        short_bars = self.polygon.get_option_bars(
            short_occ, entry_date, expiration,
            multiplier=self.bar_size, timespan='minute')
        long_bars = self.polygon.get_option_bars(
            long_occ, entry_date, expiration,
            multiplier=self.bar_size, timespan='minute')

        if not short_bars and not long_bars:
            return {}, [], 0

        # Index by timestamp for alignment
        short_by_ts = {bar['t']: bar for bar in short_bars}
        long_by_ts = {bar['t']: bar for bar in long_bars}

        # Find bars where BOTH legs have data at the same timestamp
        aligned_ts = sorted(set(short_by_ts.keys()) & set(long_by_ts.keys()))

        intraday_bars = []
        for ts in aligned_ts:
            s = short_by_ts[ts]
            l = long_by_ts[ts]
            spread_close = s['c'] - l['c']
            intraday_bars.append({
                'ts': ts,
                'date': ts_to_date(ts),
                'time_et': ts_to_time(ts),
                'spread_close': spread_close,
                'short_close': s['c'],
                'long_close': l['c'],
            })

        # Build daily summaries (last bar of each day = daily close)
        daily_values = {}
        for bar in intraday_bars:
            d = bar['date']
            daily_values[d] = {'close': bar['spread_close']}
            # last bar per day wins (they're sorted ascending)

        return daily_values, intraday_bars, len(intraday_bars)

    def _calc_position_size(self, spread_width, credit):
        max_risk_per = (spread_width - credit) * 100
        if max_risk_per <= 0: return 1
        max_risk_allowed = self.balance * self.position_size_pct
        max_contracts = int(max_risk_allowed / max_risk_per)
        return max(1, min(max_contracts, MAX_CONTRACTS))

    # ── v3.3.1: ROLLING LOGIC ──

    def _find_roll_target(self, pos, current_date):
        """Find a new expiration and strikes for rolling a losing position."""
        symbol = pos.symbol
        direction = pos.direction
        spot = pos.spot_at_entry  # Use original spot as reference

        # Find next weekly expiry 7-14 days from current date
        new_exp = get_weekly_expiration(current_date, ROLL_NEW_MIN_DTE, ROLL_NEW_MAX_DTE)
        if not new_exp:
            return None, "No valid expiry 7-14 days out"

        # v3.4.6: Holiday blackout — don't roll into a period with a major holiday cluster
        if self.holiday_blackout:
            clusters_only = (self.holiday_blackout == 'clusters')
            has_holiday, holiday_date = has_holiday_in_range(
                current_date.strftime('%Y-%m-%d'), new_exp, clusters_only=clusters_only)
            if has_holiday:
                mode_label = "cluster" if clusters_only else "any"
                logger.info(f"      ❌ Holiday blackout ({mode_label}): won't roll into {new_exp} (holiday {holiday_date})")
                return None, f"Holiday blackout ({holiday_date})"

        # Same as original: look up strikes, use same or further-OTM placement
        strikes = self.tradier.get_strikes(symbol, new_exp)
        if not strikes or len(strikes) < 5:
            if spot < 50: inc = 1
            elif spot < 200: inc = 2.5
            elif spot < 500: inc = 5
            else: inc = 10
            base = round(spot / inc) * inc
            strikes = [base + i * inc for i in range(-30, 31)]

        if direction == 'bullish':
            # Roll-away: move short strike further OTM (lower for puts)
            old_short = pos.short_strike
            roll_target = min(old_short, spot * 0.96)  # At least as far OTM or 4% below
            short_strike = find_nearest_strike(roll_target, [s for s in strikes if s < spot])
            if not short_strike:
                return None, "No valid short strike"
            target_width = pos.spread_width  # Keep same width
            long_strike = find_nearest_strike(short_strike - target_width,
                                              [s for s in strikes if s < short_strike])
            if not long_strike:
                return None, "No valid long strike"
            spread_width = short_strike - long_strike
            if spread_width <= 0:
                return None, "Invalid spread width"
            short_occ = build_occ_symbol(symbol, new_exp, 'P', short_strike)
            long_occ = build_occ_symbol(symbol, new_exp, 'P', long_strike)
        else:
            old_short = pos.short_strike
            roll_target = max(old_short, spot * 1.04)  # At least as far OTM or 4% above
            short_strike = find_nearest_strike(roll_target, [s for s in strikes if s > spot])
            if not short_strike:
                return None, "No valid short strike"
            target_width = pos.spread_width
            long_strike = find_nearest_strike(short_strike + target_width,
                                              [s for s in strikes if s > short_strike])
            if not long_strike:
                return None, "No valid long strike"
            spread_width = long_strike - short_strike
            if spread_width <= 0:
                return None, "Invalid spread width"
            short_occ = build_occ_symbol(symbol, new_exp, 'C', short_strike)
            long_occ = build_occ_symbol(symbol, new_exp, 'C', long_strike)

        return {
            'expiration': new_exp,
            'short_strike': short_strike,
            'long_strike': long_strike,
            'spread_width': spread_width,
            'short_occ': short_occ,
            'long_occ': long_occ,
        }, None

    def _execute_roll(self, pos, current_spread_value, current_date, date_str):
        """
        Attempt to roll a losing position. Returns True if successful.
        Close old spread at current value, open new spread at later expiry.
        """
        self.roll_stats['attempted'] += 1

        # Find roll target
        roll_target, reason = self._find_roll_target(pos, current_date)
        if not roll_target:
            self.roll_stats['skipped'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: Roll skipped — {reason}")
            return False

        # Get new spread pricing from Polygon
        new_daily, new_bars, new_bar_count = self._get_spread_price_history(
            roll_target['short_occ'], roll_target['long_occ'],
            date_str, roll_target['expiration'])

        if not new_daily:
            self.roll_stats['skipped'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: Roll skipped — no Polygon pricing for new spread")
            return False

        # New credit = first available day's close value
        first_date = min(new_daily.keys())
        new_raw_credit = new_daily[first_date]['close']

        if new_raw_credit <= 0:
            self.roll_stats['skipped'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: Roll skipped — no credit on new spread (${new_raw_credit:.2f})")
            return False

        # v3.4.1: Roll pricing based on execution model
        opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
        if self.execution_model == 'calibrated' and self.ivol:
            # Measure half-spread at close time for old spread
            close_cal = self._ivol_calibrated_slippage(
                pos.symbol, date_str, pos.expiration,
                pos.short_strike, pos.long_strike, opt_type, '15:30')
            # Measure half-spread at open time for new spread
            open_cal = self._ivol_calibrated_slippage(
                pos.symbol, date_str, roll_target['expiration'],
                roll_target['short_strike'], roll_target['long_strike'], opt_type, '15:30')
            
            close_slip = (close_cal * 2) if close_cal is not None else (self.slippage_per_leg * 2)
            open_slip = (open_cal * 2) if open_cal is not None else (self.slippage_per_leg * 2)
            close_cost = current_spread_value + close_slip
            new_credit = new_raw_credit - open_slip
        else:
            # v3.3.2 behavior: fixed slippage
            close_slippage = self.slippage_per_leg * 2
            open_slippage = self.slippage_per_leg * 2
            close_cost = current_spread_value + close_slippage
            new_credit = new_raw_credit - open_slippage

        if new_credit <= 0:
            self.roll_stats['skipped'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: Roll skipped — new credit wiped by slippage")
            return False

        net_credit = new_credit - close_cost

        # Log the roll
        logger.info(f"   🔄 ✅ {pos.symbol}: ROLLED {pos.expiration} → {roll_target['expiration']} "
                   f"| Close @ ${close_cost:.2f} | New @ ${new_credit:.2f} | Net: ${net_credit:+.2f}"
                   f" | Strikes: {pos.short_strike}/{pos.long_strike} → "
                   f"{roll_target['short_strike']}/{roll_target['long_strike']}")

        # Record roll history
        pos.roll_history.append({
            'date': date_str,
            'old_exp': pos.expiration,
            'new_exp': roll_target['expiration'],
            'close_cost': close_cost,
            'new_credit': new_credit,
            'net_credit': net_credit,
            'old_strikes': f"{pos.short_strike}/{pos.long_strike}",
            'new_strikes': f"{roll_target['short_strike']}/{roll_target['long_strike']}",
        })

        # Update position
        pos.expiration = roll_target['expiration']
        pos.short_strike = roll_target['short_strike']
        pos.long_strike = roll_target['long_strike']
        pos.spread_width = roll_target['spread_width']
        pos.short_occ = roll_target['short_occ']
        pos.long_occ = roll_target['long_occ']
        pos.total_credits += net_credit
        pos.credit = new_credit  # TP now based on new credit

        # v3.3.2 FIX: Apply roll cost to balance immediately
        roll_cost_dollars = net_credit * pos.quantity * 100
        self.balance += roll_cost_dollars
        pos.roll_cost_total += roll_cost_dollars
        # v3.4.1: Roll commission (close 2 legs + open 2 legs = 4 legs)
        self._apply_commission(4, pos.quantity)
        pos.max_loss = (roll_target['spread_width'] - new_credit) * pos.quantity * 100
        pos.daily_values = new_daily
        pos.intraday_bars = new_bars
        pos.polygon_bar_count += new_bar_count
        pos.roll_count += 1
        # Reset MAE for new spread
        pos.mae = 0
        pos.mae_date = None

        self.roll_stats['completed'] += 1
        self.roll_stats['total_net_credit'] += net_credit * pos.quantity * 100

        return True

    # ── v3.4.9: CONDITIONAL IRON CONDOR LOGIC ──

    def _build_opposite_spread(self, pos, current_date, date_str):
        """Build the opposite credit spread to convert a directional spread into an IC.

        For BPS original → build BCS opposite (and vice versa).
        Returns spread dict or None.
        """
        symbol = pos.symbol
        spot = pos.spot_at_entry  # Use current proxy from entry (stable reference)

        # Determine opposite direction
        if pos.original_direction == 'bullish':
            opp_direction = 'bearish'  # Add a BCS
        else:
            opp_direction = 'bullish'  # Add a BPS

        # Determine expiration for opposite leg
        if self.ic_dte_mode == 'same_exp':
            target_exp = pos.expiration
        else:
            # fresh_dte: find new expiration at ic_dte_min to ic_dte_max from trigger date
            target_exp = get_weekly_expiration(current_date, self.ic_dte_min, self.ic_dte_max)
            if not target_exp:
                return None

        # Holiday blackout check
        if self.holiday_blackout:
            clusters_only = (self.holiday_blackout == 'clusters')
            has_holiday, holiday_date = has_holiday_in_range(
                date_str, target_exp, clusters_only=clusters_only)
            if has_holiday:
                return None

        # Get strikes
        strikes = self.tradier.get_strikes(symbol, target_exp)
        if not strikes or len(strikes) < 5:
            if spot < 50: inc = 1
            elif spot < 200: inc = 2.5
            elif spot < 500: inc = 5
            else: inc = 10
            base = round(spot / inc) * inc
            strikes = [base + i * inc for i in range(-30, 31)]

        target_width = max(3, min(10, spot * 0.01))

        if opp_direction == 'bullish':
            # BPS: short put below spot, long put further below
            short_target = spot * 0.97
            short_strike = find_nearest_strike(short_target, [s for s in strikes if s < spot])
            if not short_strike: return None
            long_target = short_strike - target_width
            long_strike = find_nearest_strike(long_target, [s for s in strikes if s < short_strike])
            if not long_strike: return None
            spread_width = short_strike - long_strike
            if spread_width <= 0: return None
            short_occ = build_occ_symbol(symbol, target_exp, 'P', short_strike)
            long_occ = build_occ_symbol(symbol, target_exp, 'P', long_strike)
        else:
            # BCS: short call above spot, long call further above
            short_target = spot * 1.03
            short_strike = find_nearest_strike(short_target, [s for s in strikes if s > spot])
            if not short_strike: return None
            long_target = short_strike + target_width
            long_strike = find_nearest_strike(long_target, [s for s in strikes if s > short_strike])
            if not long_strike: return None
            spread_width = long_strike - short_strike
            if spread_width <= 0: return None
            short_occ = build_occ_symbol(symbol, target_exp, 'C', short_strike)
            long_occ = build_occ_symbol(symbol, target_exp, 'C', long_strike)

        return {
            'expiration': target_exp,
            'short_strike': short_strike,
            'long_strike': long_strike,
            'spread_width': spread_width,
            'short_occ': short_occ,
            'long_occ': long_occ,
            'direction': opp_direction,
        }

    def _check_ic_trigger(self, pos, date_str, current_date, actionable_signals):
        """Check if the opposite leg trigger fires for a conditional IC position.

        Returns True if triggered and opposite leg was successfully added.
        """
        if not self.conditional_ic:
            return False
        if not pos.is_conditional_ic:
            return False
        if pos.has_opposite_leg:
            return False

        self.ic_stats['checked'] += 1
        triggered = False

        if self.ic_trigger == 'flow_reversal':
            # Check if flow sentiment for this symbol has flipped
            for symbol, profile in actionable_signals:
                if symbol != pos.symbol:
                    continue
                original_dir = pos.original_direction
                current_dir = profile['direction']
                if original_dir != current_dir:
                    triggered = True
                    logger.info(f"   🔄 IC TRIGGER: {pos.symbol} flow reversed "
                               f"{original_dir} → {current_dir}")
                break

        elif self.ic_trigger == 'price_move':
            # Check if underlying moved X% against the spread
            # Get current spot from today's bars
            todays_bars = [b for b in pos.intraday_bars if b['date'] == date_str]
            if todays_bars:
                # Use spread value change as proxy for spot direction
                # For BPS (bullish): adverse = spot drops → spread increases
                # For BCS (bearish): adverse = spot rises → spread increases
                current_spread = todays_bars[-1]['spread_close']
                if pos.entry_spot > 0:
                    # We need actual spot price — check flow signals
                    for symbol, profile in actionable_signals:
                        if symbol != pos.symbol:
                            continue
                        current_spot = profile['spot_price']
                        if current_spot > 0:
                            pct_move = (current_spot - pos.entry_spot) / pos.entry_spot
                            if pos.original_direction == 'bullish' and pct_move <= -(self.ic_price_move_pct / 100):
                                triggered = True
                                logger.info(f"   🔄 IC TRIGGER: {pos.symbol} price move "
                                           f"{pct_move:+.1%} (threshold: -{self.ic_price_move_pct}%)")
                            elif pos.original_direction == 'bearish' and pct_move >= (self.ic_price_move_pct / 100):
                                triggered = True
                                logger.info(f"   🔄 IC TRIGGER: {pos.symbol} price move "
                                           f"{pct_move:+.1%} (threshold: +{self.ic_price_move_pct}%)")
                        break

        elif self.ic_trigger == 'spread_sl':
            # Check if spread mark hit X× credit
            todays_bars = [b for b in pos.intraday_bars if b['date'] == date_str]
            if todays_bars:
                current_spread = todays_bars[-1]['spread_close']
                sl_level = pos.credit * self.ic_spread_sl_mult
                if current_spread >= sl_level:
                    triggered = True
                    logger.info(f"   🔄 IC TRIGGER: {pos.symbol} spread SL "
                               f"({current_spread:.2f} >= {sl_level:.2f} = "
                               f"{self.ic_spread_sl_mult}x credit)")

        if not triggered:
            return False

        self.ic_stats['triggered'] += 1

        # Build opposite spread
        opp_spread = self._build_opposite_spread(pos, current_date, date_str)
        if not opp_spread:
            self.ic_stats['failed'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: IC opposite leg failed — no valid spread")
            return False

        # Get pricing for opposite leg
        opp_daily, opp_bars, opp_bar_count = self._get_spread_price_history(
            opp_spread['short_occ'], opp_spread['long_occ'],
            date_str, opp_spread['expiration'])

        if not opp_daily:
            self.ic_stats['failed'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: IC opposite leg failed — no Polygon pricing")
            return False

        first_date = min(opp_daily.keys())
        raw_opp_credit = opp_daily[first_date]['close']

        if raw_opp_credit <= 0:
            self.ic_stats['failed'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: IC opposite leg failed — no credit")
            return False

        # Apply slippage
        opt_type = 'PUT' if opp_spread['direction'] == 'bullish' else 'CALL'
        if self.execution_model == 'calibrated' and self.ivol:
            cal_slip = self._ivol_calibrated_slippage(
                pos.symbol, date_str, opp_spread['expiration'],
                opp_spread['short_strike'], opp_spread['long_strike'],
                opt_type, '15:00')
            if cal_slip is not None:
                opp_credit = raw_opp_credit - (cal_slip * 2)
            else:
                opp_credit = raw_opp_credit - (self.slippage_per_leg * 2)
        else:
            opp_credit = raw_opp_credit - (self.slippage_per_leg * 2)

        if opp_credit <= 0:
            self.ic_stats['failed'] += 1
            logger.info(f"   🔄 ❌ {pos.symbol}: IC opposite credit wiped by slippage")
            return False

        # Attach opposite leg to position
        pos.has_opposite_leg = True
        pos.opposite_leg_credit = opp_credit
        pos.opposite_leg_short_strike = opp_spread['short_strike']
        pos.opposite_leg_long_strike = opp_spread['long_strike']
        pos.opposite_leg_short_occ = opp_spread['short_occ']
        pos.opposite_leg_long_occ = opp_spread['long_occ']
        pos.opposite_leg_bars = opp_bars
        pos.opposite_leg_direction = opp_spread['direction']
        pos.total_credit = pos.credit + opp_credit
        pos.ic_trigger_date = date_str
        pos.ic_trigger_type = self.ic_trigger

        # Apply entry commission for opposite leg (2 legs × quantity)
        self._apply_commission(2, pos.quantity)

        self.ic_stats['built'] += 1

        opp_strategy = 'Bull Put' if opp_spread['direction'] == 'bullish' else 'Bear Call'
        logger.info(f"   🔄 ✅ {pos.symbol}: IC CONVERTED! Added {opp_strategy} "
                   f"{opp_spread['short_strike']}/{opp_spread['long_strike']} "
                   f"@ ${opp_credit:.2f} (total credit: ${pos.total_credit:.2f})")

        return True

    def process_day(self, date_str, alerts):
        logger.info(f"\n{'='*60}")
        logger.info(f"📅 {date_str} | Balance: ${self.balance:,.0f} | Positions: {len(self.positions)}/{self.max_positions}")
        logger.info(f"{'='*60}")

        current_date = datetime.strptime(date_str, '%Y-%m-%d')

        # === PHASE 1: Check existing positions for exits ===
        positions_to_close = []
        for pos in self.positions:
            exp_date = datetime.strptime(pos.expiration, '%Y-%m-%d')

            # v3.3: Scan intraday bars for this day
            todays_bars = [b for b in pos.intraday_bars if b['date'] == date_str]

            if todays_bars:
                # v3.4.9: For IC positions, compute combined P&L from both legs
                opp_spread_val = 0
                if pos.has_opposite_leg and pos.opposite_leg_bars:
                    opp_todays = [b for b in pos.opposite_leg_bars if b['date'] == date_str]
                    if opp_todays:
                        opp_spread_val = opp_todays[-1]['spread_close']

                # TP level based on total credit for IC, original credit otherwise
                effective_credit = pos.total_credit if pos.has_opposite_leg else pos.credit
                take_profit_level = effective_credit * self.take_profit_pct

                for bar in todays_bars:
                    spread_val = bar['spread_close']

                    # For IC positions, get matching opposite bar
                    if pos.has_opposite_leg and pos.opposite_leg_bars:
                        opp_bar_match = [b for b in pos.opposite_leg_bars
                                        if b['date'] == date_str and b['ts'] == bar['ts']]
                        if opp_bar_match:
                            opp_spread_val = opp_bar_match[0]['spread_close']

                    # Combined spread value for IC positions
                    combined_spread = spread_val + (opp_spread_val if pos.has_opposite_leg else 0)

                    if combined_spread > effective_credit:
                        adverse = (combined_spread - effective_credit) / effective_credit if effective_credit > 0 else 0
                        if adverse > pos.mae:
                            pos.mae = adverse
                            pos.mae_date = date_str

                    profit_pct = (effective_credit - combined_spread) / effective_credit if effective_credit > 0 else 0

                    # v3.4.8: Profit recapture tracking
                    if profit_pct > 0:
                        pos.was_profitable = True
                    if profit_pct > pos.peak_profit_pct:
                        pos.peak_profit_pct = profit_pct

                    # v3.4.8: Profit recapture exit — was profitable, now losing
                    if self.profit_recapture and pos.was_profitable and profit_pct < 0:
                        if self.execution_model == 'calibrated':
                            opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
                            cal_slip = self._ivol_calibrated_slippage(
                                pos.symbol, date_str, pos.expiration,
                                pos.short_strike, pos.long_strike,
                                opt_type, bar['time_et'])
                            if cal_slip is not None:
                                exit_spread_with_slippage = spread_val + (cal_slip * 2)
                            else:
                                exit_spread_with_slippage = spread_val + self.slippage_per_leg * 2
                        else:
                            exit_spread_with_slippage = spread_val + self.slippage_per_leg * 2
                        profit = (pos.credit - exit_spread_with_slippage) * pos.quantity * 100
                        pos.pnl = profit
                        pos.lifecycle_pnl = profit + pos.roll_cost_total
                        pos.exit_date = date_str
                        pos.tp_bar_time = bar['time_et']
                        pos.exit_reason = (f"Profit recapture @ {bar['time_et']} ET "
                                          f"(peak={pos.peak_profit_pct:.0%}, spread={spread_val:.2f})")
                        pos.status = 'closed'
                        positions_to_close.append(pos)
                        self._apply_commission(2, pos.quantity)
                        sign = '+' if profit >= 0 else ''
                        logger.info(f"   🔄 {pos.symbol}: PROFIT RECAPTURE @ {bar['time_et']} ET! "
                                   f"{sign}${profit:,.0f} "
                                   f"(peak={pos.peak_profit_pct:.0%}, spread={spread_val:.2f})")
                        break

                    if self.tp_mode == 'trail':
                        if not pos.trail_active and profit_pct >= self.trail_activate_pct and spread_val >= 0:
                            pos.trail_active = True
                            pos.trail_peak_profit_pct = profit_pct
                            pos.trail_peak_spread = spread_val
                            pos.trail_activation_bar = bar['time_et']
                            logger.info(f"   📈 {pos.symbol}: Trail ACTIVATED @ {bar['time_et']} ET "
                                       f"(profit={profit_pct:.0%}, spread={spread_val:.2f})")

                        if pos.trail_active and profit_pct > pos.trail_peak_profit_pct:
                            pos.trail_peak_profit_pct = profit_pct
                            pos.trail_peak_spread = spread_val

                        if pos.trail_active:
                            retrace = pos.trail_peak_profit_pct - profit_pct
                            if retrace >= self.trail_retrace_pct:
                                if self.execution_model == 'calibrated':
                                    opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
                                    cal_slip = self._ivol_calibrated_slippage(
                                        pos.symbol, date_str, pos.expiration,
                                        pos.short_strike, pos.long_strike,
                                        opt_type, bar['time_et'])
                                    if cal_slip is not None:
                                        exit_spread_with_slippage = spread_val + (cal_slip * 2)
                                    else:
                                        exit_spread_with_slippage = spread_val + self.slippage_per_leg * 2
                                else:
                                    exit_spread_with_slippage = spread_val + self.slippage_per_leg * 2

                                profit = (pos.credit - exit_spread_with_slippage) * pos.quantity * 100
                                pos.pnl = profit
                                pos.lifecycle_pnl = profit + pos.roll_cost_total
                                pos.exit_date = date_str
                                pos.tp_bar_time = bar['time_et']
                                pos.exit_reason = (f"Trail stop @ {bar['time_et']} ET "
                                                  f"(peak={pos.trail_peak_profit_pct:.0%}, "
                                                  f"retrace={retrace:.0%}, spread={spread_val:.2f})")
                                pos.status = 'closed'
                                positions_to_close.append(pos)
                                exit_comm = self._apply_commission(2, pos.quantity)
                                sign = '+' if profit >= 0 else ''
                                logger.info(f"   📉 {pos.symbol}: TRAIL STOP @ {bar['time_et']} ET! "
                                           f"{sign}${profit:,.0f} "
                                           f"(peak={pos.trail_peak_profit_pct:.0%}, "
                                           f"retrace={retrace:.0%}, "
                                           f"exit={exit_spread_with_slippage:.2f})")
                                break

                    else:
                        if combined_spread <= take_profit_level and combined_spread >= 0:
                            # Calculate exit slippage for original leg
                            if self.execution_model == 'calibrated':
                                opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
                                cal_slip = self._ivol_calibrated_slippage(
                                    pos.symbol, date_str, pos.expiration,
                                    pos.short_strike, pos.long_strike,
                                    opt_type, bar['time_et'])
                                if cal_slip is not None:
                                    exit_spread_with_slippage = spread_val + (cal_slip * 2)
                                else:
                                    exit_spread_with_slippage = spread_val + self.slippage_per_leg * 2
                            else:
                                exit_slippage = self.slippage_per_leg * 2
                                exit_spread_with_slippage = spread_val + exit_slippage

                            profit = (pos.credit - exit_spread_with_slippage) * pos.quantity * 100

                            # v3.4.9: Add opposite leg P&L for IC positions
                            opp_profit = 0
                            if pos.has_opposite_leg:
                                opp_exit = opp_spread_val + self.slippage_per_leg * 2
                                opp_profit = (pos.opposite_leg_credit - opp_exit) * pos.quantity * 100
                                profit += opp_profit
                                self._apply_commission(2, pos.quantity)  # Close opposite leg

                            pos.pnl = profit
                            pos.lifecycle_pnl = profit + pos.roll_cost_total
                            pos.exit_date = date_str
                            pos.tp_bar_time = bar['time_et']
                            ic_tag = " [IC]" if pos.has_opposite_leg else ""
                            pos.exit_reason = f"TP 50% @ {bar['time_et']} ET (spread={combined_spread:.2f}){ic_tag}"
                            pos.status = 'closed'
                            positions_to_close.append(pos)
                            exit_comm = self._apply_commission(2, pos.quantity)
                            sign = '+' if profit >= 0 else ''
                            logger.info(f"   💰 {pos.symbol}: TP @ {bar['time_et']} ET! "
                                       f"{sign}${profit:,.0f} "
                                       f"(spread={combined_spread:.2f}, "
                                       f"exit={exit_spread_with_slippage:.2f}){ic_tag}")
                            break

                if pos.status == 'closed':
                    continue

                # Also track MAE from remaining bars (after TP check didn't trigger)
                # Already done in the loop above

            # v3.3.1: ROLLING CHECK — before stop/expiration, try to roll losers
            # Matches V16.6.1: rolling gets first chance to save the position
            if self.rolling_enabled and pos.status != 'closed':
                exp_date_dt = datetime.strptime(pos.expiration, '%Y-%m-%d')
                dte = (exp_date_dt - current_date).days

                if dte <= self.roll_max_dte and pos.roll_count < ROLL_MAX_PER_POSITION:
                    # Get current spread value
                    roll_spread_value = None
                    todays_for_roll = [b for b in pos.intraday_bars if b['date'] == date_str]
                    if todays_for_roll:
                        roll_spread_value = todays_for_roll[-1]['spread_close']
                    elif pos.daily_values and date_str in pos.daily_values:
                        roll_spread_value = pos.daily_values[date_str]['close']

                    if roll_spread_value is not None and roll_spread_value > pos.credit * ROLL_TRIGGER_MULTIPLIER:
                        # Position is losing badly with low DTE — try to roll
                        if self._execute_roll(pos, roll_spread_value, current_date, date_str):
                            continue  # Position rolled — skip stop/expiration check

            # v3.3.1: CREDIT STOP CHECK — only if rolling didn't fire
            # Emergency stop: when DTE > roll_dte (too far to roll), apply credit stop
            if self.credit_stop_multiplier > 0 and pos.status != 'closed':
                exp_date_for_stop = datetime.strptime(pos.expiration, '%Y-%m-%d')
                dte_for_stop = (exp_date_for_stop - current_date).days
                # Only apply stop when DTE > roll threshold (position can't be rolled yet)
                if dte_for_stop > self.roll_max_dte:
                    # Get current spread value (last bar today or last known)
                    stop_value = None
                    todays_for_stop = [b for b in pos.intraday_bars if b['date'] == date_str]
                    if todays_for_stop:
                        stop_value = todays_for_stop[-1]['spread_close']
                    elif pos.daily_values and date_str in pos.daily_values:
                        stop_value = pos.daily_values[date_str]['close']

                    if stop_value is not None:
                        stop_level = pos.credit * self.credit_stop_multiplier
                        if stop_value >= stop_level:
                            # v3.4.1: Exit pricing based on execution model
                            if self.execution_model == 'calibrated':
                                opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
                                cal_slip = self._ivol_calibrated_slippage(
                                    pos.symbol, date_str, pos.expiration,
                                    pos.short_strike, pos.long_strike, opt_type, '15:30')
                                if cal_slip is not None:
                                    exit_cost = stop_value + (cal_slip * 2)
                                else:
                                    exit_cost = stop_value + self.slippage_per_leg * 2
                            else:
                                exit_slippage = self.slippage_per_leg * 2
                                exit_cost = stop_value + exit_slippage
                            profit = (pos.credit - exit_cost) * pos.quantity * 100
                            max_loss = -(pos.spread_width - pos.credit) * pos.quantity * 100
                            saved_vs_max = profit - max_loss
                            pos.pnl = profit
                            pos.lifecycle_pnl = profit + pos.roll_cost_total
                            pos.exit_date = date_str
                            pos.exit_reason = f"Emergency stop {self.credit_stop_multiplier:.1f}x DTE={dte_for_stop} (spread={stop_value:.2f} >= {stop_level:.2f})"
                            pos.status = 'closed'
                            positions_to_close.append(pos)
                            self._apply_commission(2, pos.quantity)  # v3.4
                            self.stop_stats['triggered'] += 1
                            self.stop_stats['total_saved_vs_max'] += saved_vs_max
                            logger.info(f"   🛑 {pos.symbol}: EMERGENCY STOP {self.credit_stop_multiplier:.1f}x "
                                       f"DTE={dte_for_stop} ${profit:+,.0f} (spread={stop_value:.2f}, "
                                       f"saved ${saved_vs_max:+,.0f} vs max loss)")
                            continue

            # TIME-BASED STALE EXIT: after N trading days, close at market value
            if self.max_hold_days > 0 and pos.status != 'closed':
                # v3.4.5: If stale_rolled_only, skip non-rolled positions
                if self.stale_rolled_only and pos.roll_count == 0:
                    pass  # Let non-rolled positions keep running
                else:
                    entry_dt = datetime.strptime(pos.entry_date, '%Y-%m-%d').date()
                    current_dt = current_date.date() if hasattr(current_date, 'date') else current_date
                    # Count business days held (exclude entry day)
                    days_held = 0
                    check = entry_dt + timedelta(days=1)
                    while check <= current_dt:
                        if check.weekday() < 5:  # Mon-Fri
                            days_held += 1
                        check += timedelta(days=1)
                    
                    if days_held >= self.max_hold_days:
                        # Get current spread value
                        if date_str in pos.daily_values:
                            stale_value = pos.daily_values[date_str]['close']
                        elif pos.daily_values:
                            last_date = max(pos.daily_values.keys())
                            stale_value = pos.daily_values[last_date]['close']
                        else:
                            stale_value = pos.credit  # Assume break-even if no data
                        
                        # v3.4.1: Exit pricing based on execution model
                        if self.execution_model == 'calibrated':
                            opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
                            cal_slip = self._ivol_calibrated_slippage(
                                pos.symbol, date_str, pos.expiration,
                                pos.short_strike, pos.long_strike, opt_type, '15:45')
                            if cal_slip is not None:
                                exit_with_slippage = max(0, stale_value) + (cal_slip * 2)
                            else:
                                exit_with_slippage = max(0, stale_value) + self.slippage_per_leg * 2
                        else:
                            exit_slippage = self.slippage_per_leg * 2
                            exit_with_slippage = max(0, stale_value) + exit_slippage
                        profit = (pos.credit - exit_with_slippage) * pos.quantity * 100
                        # Estimate what full expiration loss would have been
                        full_loss = -(pos.spread_width - pos.credit) * pos.quantity * 100
                        saved = profit - full_loss  # positive = we saved money vs max loss
                        
                        pos.pnl = profit
                        pos.lifecycle_pnl = profit + pos.roll_cost_total  # v3.3.2: includes roll costs
                        pos.exit_date = date_str
                        rolled_tag = " [ROLLED]" if pos.roll_count > 0 else ""
                        pos.exit_reason = f"Stale exit day {days_held}{rolled_tag} (spread={stale_value:.2f}, credit={pos.credit:.2f})"
                        pos.status = 'closed'
                        positions_to_close.append(pos)
                        self._apply_commission(2, pos.quantity)  # v3.4
                        self.stale_exits += 1
                        self.stale_saved += saved
                        logger.info(f"   ⏰ {pos.symbol}: STALE EXIT day {days_held}{rolled_tag} "
                                   f"${profit:+,.0f} (spread={stale_value:.2f}, credit={pos.credit:.2f}, "
                                   f"saved ${saved:+,.0f} vs max loss)")
                        continue

            # Expiration check
            if current_date >= exp_date:
                # Use daily close from last available bar
                if date_str in pos.daily_values:
                    final = pos.daily_values[date_str]['close']
                elif pos.daily_values:
                    last_date = max(pos.daily_values.keys())
                    final = pos.daily_values[last_date]['close']
                else:
                    final = 0
                # v3.4.1: Exit pricing based on execution model
                if self.execution_model == 'calibrated':
                    opt_type = 'PUT' if pos.direction == 'bullish' else 'CALL'
                    cal_slip = self._ivol_calibrated_slippage(
                        pos.symbol, date_str, pos.expiration,
                        pos.short_strike, pos.long_strike, opt_type, '15:45')
                    if cal_slip is not None:
                        final_with_slippage = max(0, final) + (cal_slip * 2)
                    else:
                        final_with_slippage = max(0, final) + self.slippage_per_leg * 2
                else:
                    exit_slippage = self.slippage_per_leg * 2
                    final_with_slippage = max(0, final) + exit_slippage
                profit = (pos.credit - final_with_slippage) * pos.quantity * 100
                # v3.4.9: Add opposite leg P&L for IC positions at expiration
                if pos.has_opposite_leg:
                    opp_final = 0
                    if pos.opposite_leg_bars:
                        opp_todays = [b for b in pos.opposite_leg_bars if b['date'] == date_str]
                        if opp_todays:
                            opp_final = opp_todays[-1]['spread_close']
                        else:
                            # Use last known bar
                            opp_final = pos.opposite_leg_bars[-1]['spread_close'] if pos.opposite_leg_bars else 0
                    opp_exit = max(0, opp_final) + self.slippage_per_leg * 2
                    opp_profit = (pos.opposite_leg_credit - opp_exit) * pos.quantity * 100
                    profit += opp_profit
                    self._apply_commission(2, pos.quantity)  # Close opposite leg
                pos.pnl = profit
                pos.lifecycle_pnl = profit + pos.roll_cost_total  # v3.3.2: includes roll costs
                pos.exit_date = date_str
                ic_tag = " [IC]" if pos.has_opposite_leg else ""
                pos.exit_reason = f"Expiration{ic_tag}"
                pos.status = 'closed'
                positions_to_close.append(pos)
                self._apply_commission(2, pos.quantity)  # v3.4.1: exit commission
                logger.info(f"   📋 {pos.symbol}: Expired{ic_tag}, P&L: ${profit:+,.0f}")

        for pos in positions_to_close:
            self.positions.remove(pos)
            self.closed_positions.append(pos)
            self.balance += pos.pnl

        # === PHASE 1.5: Conditional IC trigger check ===
        if self.conditional_ic:
            # Build a quick signal snapshot for trigger checking
            ic_aggregator = SignalAggregator()
            for alert in alerts:
                ic_aggregator.add_alert(alert)
            # Use mid-day scan time for signal snapshot
            if alerts:
                mid_time = alerts[0].timestamp.replace(hour=17, minute=0, second=0)
                ic_actionable = ic_aggregator.get_actionable(mid_time)
            else:
                ic_actionable = []

            for pos in self.positions:
                if pos.is_conditional_ic and not pos.has_opposite_leg and pos.status != 'closed':
                    self._check_ic_trigger(pos, date_str, current_date, ic_actionable)

        # === PHASE 2: Rolling window signal generation ===
        open_slots = self.max_positions - len(self.positions)
        if open_slots <= 0:
            logger.info(f"   ⏳ No open slots ({len(self.positions)}/{self.max_positions})")
        else:
            alerts.sort(key=lambda a: a.timestamp)
            if not alerts:
                self.equity_curve.append(self._snapshot(date_str))
                self.trading_days.append(date_str)
                return

            day_start = alerts[0].timestamp.replace(hour=14, minute=30, second=0)
            day_end = alerts[0].timestamp.replace(
                hour=ENTRY_CUTOFF_HOUR_UTC,
                minute=ENTRY_CUTOFF_MINUTE_UTC, second=0)

            scan_time = day_start
            aggregator = SignalAggregator()
            symbols_traded_today = set()
            symbols_with_positions = {p.symbol for p in self.positions}
            traded_this_day = False

            # VIX hysteresis — updated per scan time below (not once per day)

            for alert in alerts:
                aggregator.add_alert(alert)

            while scan_time <= day_end:
                if open_slots <= 0:
                    break

                # VIX hysteresis check at each scan time (intraday)
                if self.vix_block_bps > 0:
                    scan_min_utc = scan_time.hour * 60 + scan_time.minute
                    vix_now = self._get_vix_at_time(date_str, scan_min_utc)
                    if vix_now > 0:
                        if self.vix_bps_blocked and vix_now < self.vix_unblock_bps:
                            self.vix_bps_blocked = False
                            logger.info(f"   📉 VIX {vix_now:.1f} < {self.vix_unblock_bps} @ "
                                       f"{scan_time.strftime('%H:%M')} — BPS re-enabled")
                        elif not self.vix_bps_blocked and vix_now > self.vix_block_bps:
                            self.vix_bps_blocked = True
                            logger.info(f"   📈 VIX {vix_now:.1f} > {self.vix_block_bps} @ "
                                       f"{scan_time.strftime('%H:%M')} — BPS BLOCKED")

                actionable = aggregator.get_actionable(scan_time)

                for symbol, profile in actionable:
                    if open_slots <= 0:
                        break
                    if symbol in symbols_with_positions or symbol in symbols_traded_today:
                        continue

                    spot = profile['spot_price']
                    if spot <= 0:
                        continue

                    direction = profile['direction']
                    conviction = profile['conviction_ratio']
                    weighted_prem = profile['dominant_premium']

                    # Put-only mode: skip all bearish (BCS) signals
                    if self.put_only and direction == 'bearish':
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol,
                            'reason': 'Put-only mode (BCS skipped)',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    # Market bias BPS blocking: block BPS when market is heavily bearish
                    if self.market_bias_block > 0 and direction == 'bullish':
                        # Compute market-wide bear % from all actionable signals
                        total_bull = sum(p['bullish_premium'] for _, p in actionable)
                        total_bear = sum(p['bearish_premium'] for _, p in actionable)
                        total_flow = total_bull + total_bear
                        if total_flow > 0:
                            bear_pct = total_bear / total_flow
                            if bear_pct > self.market_bias_block:
                                self.rejected_signals.append({
                                    'date': date_str, 'symbol': symbol,
                                    'reason': f'Market bias block (bear {bear_pct:.0%} > {self.market_bias_block:.0%})',
                                    'time': scan_time.strftime('%H:%M')})
                                continue

                    # VIX hysteresis BPS blocking
                    if self.vix_bps_blocked and direction == 'bullish':
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol,
                            'reason': f'VIX BPS blocked (VIX > {self.vix_block_bps})',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    # BCS preference: in bearish markets, flip bullish signals to bearish (BCS)
                    if self.bcs_preference_threshold > 0 and direction == 'bullish':
                        total_bull = sum(p['bullish_premium'] for _, p in actionable)
                        total_bear = sum(p['bearish_premium'] for _, p in actionable)
                        total_flow = total_bull + total_bear
                        if total_flow > 0:
                            bear_pct = total_bear / total_flow
                            if bear_pct > self.bcs_preference_threshold:
                                direction = 'bearish'
                                logger.info(f"   🔄 {symbol}: BCS preference — flipped to bearish "
                                           f"(bear {bear_pct:.0%} > {self.bcs_preference_threshold:.0%})")

                    if not traded_this_day:
                        logger.info(f"   🔍 Scanning at {scan_time.strftime('%H:%M')} UTC...")
                        traded_this_day = True

                    logger.info(f"   🐋 {symbol}: {direction} {conviction:.0%} conv, "
                               f"${weighted_prem/1000:.0f}K wprem, spot=${spot:.2f} "
                               f"@ {scan_time.strftime('%H:%M')}")

                    spread = self._find_spread(symbol, current_date, spot, direction)
                    if not spread:
                        logger.info(f"      ❌ No valid spread")
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol, 'reason': 'No valid spread',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    # v3.3: Fetch pricing from Polygon
                    daily_values, intraday_bars, bar_count = self._get_spread_price_history(
                        spread['short_occ'], spread['long_occ'],
                        date_str, spread['expiration'])

                    if not daily_values:
                        logger.info(f"      ❌ No Polygon pricing for {spread['short_occ']} / {spread['long_occ']}")
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol, 'reason': 'No Polygon pricing',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    entry_date_key = min(daily_values.keys())
                    raw_credit = daily_values[entry_date_key]['close']

                    if raw_credit <= 0:
                        logger.info(f"      ❌ No credit (spread = ${raw_credit:.2f})")
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol, 'reason': f'No credit ({raw_credit:.2f})',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    # v3.4.1: Determine entry slippage based on execution model
                    opt_type = 'PUT' if direction == 'bullish' else 'CALL'
                    # Convert UTC scan time to ET for iVol query
                    et_hour = scan_time.hour - 5  # Rough UTC→ET
                    entry_time_et = f"{et_hour}:{scan_time.minute:02d}"

                    if self.execution_model == 'calibrated':
                        # v3.4.1: iVol-calibrated slippage — measure real half-spread per leg
                        cal_slip = self._ivol_calibrated_slippage(
                            symbol, date_str, spread['expiration'],
                            spread['short_strike'], spread['long_strike'],
                            opt_type, entry_time_et)
                        if cal_slip is not None:
                            total_slippage = cal_slip * 2  # Both legs
                            credit = raw_credit - total_slippage
                            logger.info(f"      📊 iVol calibrated: slip=${cal_slip:.3f}/leg "
                                       f"(mid=${raw_credit:.2f}, credit=${credit:.2f})")
                        else:
                            # Fallback to fixed slippage if iVol unavailable
                            total_slippage = self.slippage_per_leg * 2
                            credit = raw_credit - total_slippage
                            logger.debug(f"      ⚠️ iVol miss for {symbol} — using fixed slippage")
                    else:
                        # v3.3.2 behavior: fixed slippage
                        total_slippage = self.slippage_per_leg * 2
                        credit = raw_credit - total_slippage

                    if credit <= 0:
                        logger.info(f"      ❌ Credit wiped by slippage "
                                   f"(raw=${raw_credit:.2f} - slip=${total_slippage:.2f} = ${credit:.2f})")
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol,
                            'reason': f'Credit wiped by slippage ({raw_credit:.2f} - {total_slippage:.2f})',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    credit_ratio = credit / spread['spread_width']
                    if credit_ratio < self.min_credit_ratio:
                        logger.info(f"      ❌ Credit ratio {credit_ratio:.0%} < {self.min_credit_ratio:.0%} "
                                   f"(after ${total_slippage:.2f} slippage)")
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol,
                            'reason': f'Credit ratio {credit_ratio:.0%} < {self.min_credit_ratio:.0%} after slippage',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    # v3.4.5: Max credit ratio cap — high credit/width = near-the-money = roll-prone
                    if self.max_credit_ratio < 1.0 and credit_ratio > self.max_credit_ratio:
                        logger.info(f"      ❌ Credit ratio {credit_ratio:.0%} > {self.max_credit_ratio:.0%} cap "
                                   f"(near-the-money filter)")
                        self.rejected_signals.append({
                            'date': date_str, 'symbol': symbol,
                            'reason': f'Credit ratio {credit_ratio:.0%} > {self.max_credit_ratio:.0%} cap',
                            'time': scan_time.strftime('%H:%M')})
                        continue

                    quantity = self._calc_position_size(spread['spread_width'], credit)

                    pos = Position(
                        symbol=symbol, direction=direction, entry_date=date_str,
                        short_strike=spread['short_strike'], long_strike=spread['long_strike'],
                        expiration=spread['expiration'], short_occ=spread['short_occ'],
                        long_occ=spread['long_occ'], credit=credit,
                        spread_width=spread['spread_width'], quantity=quantity,
                        spot_at_entry=spot, conviction=conviction,
                        weighted_premium=weighted_prem,
                        signal_time=scan_time.strftime('%H:%M'),
                        slippage_applied=total_slippage,
                    )
                    pos.daily_values = daily_values
                    pos.intraday_bars = intraday_bars
                    pos.polygon_data_found = True
                    pos.polygon_bar_count = bar_count
                    pos.tradier_data_found = True
                    # v3.4.9: Mark for conditional IC tracking
                    if self.conditional_ic:
                        pos.is_conditional_ic = True
                        pos.original_direction = direction
                        pos.entry_spot = spot

                    self.positions.append(pos)
                    symbols_with_positions.add(symbol)
                    symbols_traded_today.add(symbol)
                    open_slots -= 1

                    # v3.4.1: Apply entry commission (2 legs × quantity)
                    entry_comm = self._apply_commission(2, quantity)

                    strategy = 'Bull Put' if direction == 'bullish' else 'Bear Call'
                    comm_str = f", comm=${entry_comm:.0f}" if entry_comm > 0 else ""
                    logger.info(f"      ✅ OPENED: {symbol} {strategy} "
                               f"{spread['short_strike']}/{spread['long_strike']} "
                               f"x{quantity} @ ${credit:.2f} credit "
                               f"(raw=${raw_credit:.2f}, slip=${total_slippage:.2f}, "
                               f"ratio={credit_ratio:.0%}, DTE={pos.dte_at_entry}, "
                               f"bars={bar_count}{comm_str})")

                    self.all_signals.append({
                        'date': date_str, 'symbol': symbol, 'direction': direction,
                        'conviction': conviction, 'weighted_premium': weighted_prem,
                        'spot': spot, 'short_strike': spread['short_strike'],
                        'long_strike': spread['long_strike'], 'expiration': spread['expiration'],
                        'credit': credit, 'raw_credit': raw_credit,
                        'slippage': total_slippage,
                        'spread_width': spread['spread_width'],
                        'credit_ratio': credit_ratio, 'quantity': quantity,
                        'signal_time': scan_time.strftime('%H:%M'),
                        'bar_count': bar_count,
                    })

                scan_time += timedelta(minutes=self.scan_interval)

        # Record equity
        self.equity_curve.append(self._snapshot(date_str))
        self.trading_days.append(date_str)

    def _snapshot(self, date_str):
        unrealized = 0
        for pos in self.positions:
            if pos.daily_values and date_str in pos.daily_values:
                current_spread = pos.daily_values[date_str]['close']
                unrealized += (pos.credit - current_spread) * pos.quantity * 100
        return {
            'date': date_str,
            'balance': self.balance,
            'unrealized': unrealized,
            'total_equity': self.balance + unrealized,
            'open_positions': len(self.positions),
        }

    def force_close_remaining(self):
        for pos in list(self.positions):
            if pos.daily_values:
                last_date = max(pos.daily_values.keys())
                final = pos.daily_values[last_date]['close']
            else:
                final = pos.credit
            exit_slippage = self.slippage_per_leg * 2
            final_with_slippage = max(0, final) + exit_slippage
            profit = (pos.credit - final_with_slippage) * pos.quantity * 100
            pos.pnl = profit
            pos.lifecycle_pnl = profit + pos.roll_cost_total  # v3.3.2: includes roll costs
            pos.exit_date = 'END_OF_REPLAY'
            pos.exit_reason = 'Still open (replay ended)'
            pos.status = 'force_closed'
            self.positions.remove(pos)
            self.closed_positions.append(pos)
            self.balance += profit
            logger.info(f"   📋 Force close {pos.symbol}: P&L ${profit:+,.0f}")

    def run(self, csv_paths):
        if isinstance(csv_paths, str):
            csv_paths = [csv_paths]
        
        # Load and merge all data files
        merged_alerts = defaultdict(list)
        for csv_path in csv_paths:
            alerts = self.load_flow_data(csv_path)
            for date_key, date_alerts in alerts.items():
                merged_alerts[date_key].extend(date_alerts)
        
        # Deduplicate by (date, time, symbol, direction)
        deduped = defaultdict(list)
        for date_key, date_alerts in merged_alerts.items():
            seen = set()
            for alert in date_alerts:
                key = (alert.date_key, alert.timestamp, alert.symbol, alert.sentiment)
                if key not in seen:
                    seen.add(key)
                    deduped[date_key].append(alert)
        alerts_by_date = deduped
        
        logger.info(f"   ✅ Total after dedup: {sum(len(v) for v in alerts_by_date.values())} alerts across {len(alerts_by_date)} days")

        # Load VIX data if VIX hysteresis is enabled
        if self.vix_block_bps > 0:
            all_dates = sorted(alerts_by_date.keys())
            self._load_vix_data(csv_paths, all_dates[0], all_dates[-1])

        logger.info(f"\n🚀 STARTING REPLAY v3.4.5 — REAL CALIBRATED SLIPPAGE MODEL")
        logger.info(f"   Capital: ${self.starting_capital:,.0f}")
        logger.info(f"   Max positions: {self.max_positions}")
        logger.info(f"   Position size: {self.position_size_pct:.0%}")
        logger.info(f"   Conviction: {MIN_CONVICTION:.0%}")
        logger.info(f"   Credit ratio: {self.min_credit_ratio:.0%} min"
                    + (f", {self.max_credit_ratio:.0%} max" if self.max_credit_ratio < 1.0 else ", no max cap"))
        logger.info(f"   Premium gate: ${MIN_ACTIONABLE_PREMIUM/1000:.0f}K weighted")
        logger.info(f"   Take profit: {self.take_profit_pct:.0%} of credit")
        logger.info(f"   TP mode: {self.tp_mode}")
        if self.tp_mode == 'trail':
            logger.info(f"   Trail activation: {self.trail_activate_pct:.0%} profit")
            logger.info(f"   Trail retrace: {self.trail_retrace_pct:.0%} from peak")
        logger.info(f"   ─── v3.4.1 EXECUTION MODEL ───")
        if self.execution_model == 'calibrated':
            logger.info(f"   Execution: CALIBRATED (iVol half-spread per leg)")
            logger.info(f"   Entry: sell short@bid, buy long@ask (REAL)")
            logger.info(f"   Exit: buy short@ask, sell long@bid (REAL)")
            logger.info(f"   Fallback: ${self.slippage_per_leg:.2f}/leg fixed if iVol unavailable")
        else:
            logger.info(f"   Execution: FIXED slippage ${self.slippage_per_leg:.2f}/leg")
        logger.info(f"   Commissions: {'$%.2f/leg/contract' % COMMISSION_PER_LEG if self.commissions_enabled else 'DISABLED'}")
        logger.info(f"   ─── PRICING + MONITORING ───")
        logger.info(f"   Pricing source: Polygon/Massive API ({self.bar_size}-min bars)")
        logger.info(f"   Scan interval: {self.scan_interval} min (rolling window, no future peeking)")
        logger.info(f"   TP detection: TIME-ALIGNED {self.bar_size}-min bars (both legs same timestamp)")
        logger.info(f"   Entry cutoff: {ENTRY_CUTOFF_HOUR_UTC}:{ENTRY_CUTOFF_MINUTE_UTC:02d} UTC (~3:30 PM ET)")
        logger.info(f"   Blacklist: {TICKER_BLACKLIST}")
        logger.info(f"   ─── v3.3.1 ROLLING ───")
        if self.rolling_enabled:
            logger.info(f"   Rolling: ENABLED (trigger: {ROLL_TRIGGER_MULTIPLIER}x credit, DTE<={self.roll_max_dte}, max {ROLL_MAX_PER_POSITION} rolls)")
        else:
            logger.info(f"   Rolling: DISABLED (--no-rolling)")
        if self.credit_stop_multiplier > 0:
            logger.info(f"   Credit stop: {self.credit_stop_multiplier}x credit")
        else:
            logger.info(f"   Credit stop: DISABLED")
        if self.max_hold_days > 0:
            rolled_tag = " (ROLLED ONLY)" if self.stale_rolled_only else " (ALL positions)"
            logger.info(f"   Stale exit: after {self.max_hold_days} trading day(s){rolled_tag}")
        if self.holiday_blackout:
            mode_desc = "CLUSTERS only (Thanksgiving/Christmas/NYE)" if self.holiday_blackout == 'clusters' else "ALL holidays"
            logger.info(f"   Holiday blackout: {mode_desc}")
        logger.info(f"   ─── v3.4.7 OPTIMIZATIONS ───")
        if self.market_bias_block > 0:
            logger.info(f"   Market bias BPS block: >{self.market_bias_block:.0%} bearish")
        if self.vix_block_bps > 0:
            logger.info(f"   VIX BPS gate: block >{self.vix_block_bps}, re-enable <{self.vix_unblock_bps}")
        if self.bcs_preference_threshold > 0:
            logger.info(f"   BCS preference: force BCS when bear >{self.bcs_preference_threshold:.0%}")
        if self.conditional_ic:
            logger.info(f"   ─── v3.4.9 CONDITIONAL IRON CONDOR ───")
            logger.info(f"   Trigger: {self.ic_trigger}")
            logger.info(f"   DTE mode: {self.ic_dte_mode}")
            if self.ic_trigger == 'price_move':
                logger.info(f"   Price move threshold: {self.ic_price_move_pct}%")
            elif self.ic_trigger == 'spread_sl':
                logger.info(f"   Spread SL multiplier: {self.ic_spread_sl_mult}x")

        for date_str in sorted(alerts_by_date.keys()):
            self.process_day(date_str, alerts_by_date[date_str])

        if self.positions:
            logger.info(f"\n📋 Force closing {len(self.positions)} remaining positions...")
            self.force_close_remaining()

        self.print_results()
        self.save_results()

    def print_results(self):
        all_trades = self.closed_positions
        if not all_trades:
            logger.info("\n❌ No trades executed.")
            return

        force_closed = [t for t in all_trades if t.status == 'force_closed']
        non_force = [t for t in all_trades if t.status != 'force_closed']

        # v3.3.2: Use lifecycle_pnl for classification (includes roll costs)
        winners = [t for t in all_trades if t.lifecycle_pnl > 0]
        losers = [t for t in all_trades if t.lifecycle_pnl < 0]
        breakeven = [t for t in all_trades if t.lifecycle_pnl == 0]

        total_pnl = sum(t.lifecycle_pnl for t in all_trades)
        gross_profit = sum(t.lifecycle_pnl for t in winners)
        gross_loss = sum(t.lifecycle_pnl for t in losers)  # v3.3.2: stored NEGATIVE (was abs())

        win_rate = len(winners) / len(all_trades) * 100 if all_trades else 0
        avg_win = gross_profit / len(winners) if winners else 0
        avg_loss = gross_loss / len(losers) if losers else 0  # v3.3.2: NEGATIVE number
        profit_factor = gross_profit / abs(gross_loss) if gross_loss != 0 else float('inf')
        expectancy = total_pnl / len(all_trades) if all_trades else 0

        # v3.3.2: Total roll cost tracking
        total_roll_costs = sum(t.roll_cost_total for t in all_trades)
        rolled_trades = [t for t in all_trades if t.roll_count > 0]

        total_slippage_paid = sum(t.slippage_applied * t.quantity * 100 for t in all_trades)
        total_bars = sum(t.polygon_bar_count for t in all_trades)

        peak = self.starting_capital
        max_dd = 0
        max_dd_date = ''
        for eq in self.equity_curve:
            peak = max(peak, eq['total_equity'])
            dd = (peak - eq['total_equity']) / peak
            if dd > max_dd:
                max_dd = dd
                max_dd_date = eq['date']

        trades_with_mae = [t for t in all_trades if t.mae > 0]
        avg_mae = sum(t.mae for t in trades_with_mae) / len(trades_with_mae) if trades_with_mae else 0
        max_mae = max((t.mae for t in all_trades), default=0)
        max_mae_trade = next((t for t in all_trades if t.mae == max_mae), None) if max_mae > 0 else None

        # v3.4.4: Trailing stop stats
        if self.tp_mode == 'trail':
            trail_activated = [t for t in self.closed_positions if t.trail_active]
            trail_exits = [t for t in self.closed_positions if t.exit_reason and 'Trail stop' in t.exit_reason]
            trail_peaks = [t.trail_peak_profit_pct for t in trail_activated] if trail_activated else [0]
            logger.info(f"   --- TRAILING STOP STATS (v3.4.4) ---")
            logger.info(f"   Trail activated:   {len(trail_activated):>10}")
            logger.info(f"   Trail exits:       {len(trail_exits):>10}")
            logger.info(f"   Avg peak profit:   {sum(trail_peaks)/len(trail_peaks):>10.0%}")
            logger.info(f"   Max peak profit:   {max(trail_peaks):>10.0%}")

        # TP timing stats
        tp_trades = [t for t in all_trades if t.tp_bar_time]
        avg_tp_bars = 0
        if tp_trades:
            # Count bars from entry to TP for each
            tp_bar_counts = []
            for t in tp_trades:
                entry_bars = [b for b in t.intraday_bars if b['date'] >= t.entry_date]
                for i, b in enumerate(entry_bars):
                    if b['spread_close'] <= t.credit * self.take_profit_pct and b['spread_close'] >= 0:
                        tp_bar_counts.append(i + 1)
                        break
            avg_tp_bars = sum(tp_bar_counts) / len(tp_bar_counts) if tp_bar_counts else 0

        logger.info(f"\n{'='*60}")
        logger.info(f"📊 REPLAY RESULTS v3.4.5 — {'CALIBRATED SLIPPAGE' if self.execution_model == 'calibrated' else 'FIXED SLIPPAGE'}")
        logger.info(f"{'='*60}")
        logger.info(f"   Period: {self.trading_days[0]} to {self.trading_days[-1]} ({len(self.trading_days)} days)")
        logger.info(f"   Polygon API calls: {self.polygon.api_calls}")
        logger.info(f"   Tradier API calls: {self.tradier.api_calls}")
        if self.execution_model == 'calibrated':
            logger.info(f"   iVol API calls: {self.ivol.requests_made if self.ivol else 0}")
            logger.info(f"   iVol hits/misses: {self.ivol_stats['hits']}/{self.ivol_stats['misses']}")
        logger.info(f"   ─── EXECUTION MODEL ───")
        if self.execution_model == 'calibrated':
            logger.info(f"   Model: CALIBRATED (iVol half-spread per leg)")
            logger.info(f"   Fallback slippage: ${self.slippage_per_leg:.2f}/leg when iVol miss")
        else:
            logger.info(f"   Model: FIXED slippage ${self.slippage_per_leg:.2f}/leg")
        logger.info(f"   Total slippage paid: ${total_slippage_paid:,.0f}")
        if self.commissions_enabled:
            logger.info(f"   Commissions: ${self.total_commissions:,.0f} (${COMMISSION_PER_LEG}/leg/contract)")
        else:
            logger.info(f"   Commissions: DISABLED")
        logger.info(f"   Total aligned bars: {total_bars:,}")
        logger.info(f"")
        logger.info(f"   Starting Capital:  ${self.starting_capital:>10,.0f}")
        logger.info(f"   Final Balance:     ${self.balance:>10,.0f}")
        if self.commissions_enabled:
            logger.info(f"   Total Commissions: ${self.total_commissions:>10,.0f}")
            net_return = (self.balance - self.starting_capital)
            logger.info(f"   Net Return:        {net_return/self.starting_capital:>+10.1%} (after commissions)")
        logger.info(f"")
        logger.info(f"   ─── ALL TRADES (force-closed INCLUDED) ───")
        logger.info(f"   Total Trades:      {len(all_trades):>10}")
        logger.info(f"     Completed:       {len(non_force):>10}")
        logger.info(f"     Force-closed:    {len(force_closed):>10}")
        logger.info(f"   Net P&L:           ${total_pnl:>+10,.0f}")
        logger.info(f"   Return:            {total_pnl/self.starting_capital:>+10.1%}")
        logger.info(f"   Winners:           {len(winners):>10} ({win_rate:.1f}%)")
        logger.info(f"   Losers:            {len(losers):>10}")
        logger.info(f"   Breakeven:         {len(breakeven):>10}")
        logger.info(f"   Avg Winner:        ${avg_win:>+10,.0f}")
        logger.info(f"   Avg Loser:         " + (f"${avg_loss:>+10,.0f}" if losers else "               N/A"))
        logger.info(f"   Expectancy:        ${expectancy:>+10,.0f} per trade")
        logger.info(f"   Gross Profit:      ${gross_profit:>+10,.0f}")
        logger.info(f"   Gross Loss:        ${gross_loss:>+10,.0f}")  # v3.3.2: now negative
        logger.info(f"   Profit Factor:     {profit_factor:>10.2f}")
        logger.info(f"   Max Drawdown:      {max_dd:>10.1%}" + (f" ({max_dd_date})" if max_dd_date else ""))
        logger.info(f"")
        logger.info(f"   ─── MAX ADVERSE EXCURSION (MAE) — time-aligned ───")
        logger.info(f"   Trades with MAE>0: {len(trades_with_mae):>10}")
        logger.info(f"   Avg MAE (of those):{avg_mae:>10.1%} of credit")
        if max_mae_trade:
            logger.info(f"   Worst MAE:         {max_mae:>10.1%} ({max_mae_trade.symbol} on {max_mae_trade.mae_date})")
        logger.info(f"")
        logger.info(f"   ─── TP TIMING (v3.3 time-aligned) ───")
        logger.info(f"   Trades hit TP:     {len(tp_trades):>10}")
        logger.info(f"   Avg bars to TP:    {avg_tp_bars:>10.0f} ({self.bar_size}-min bars)")
        if self.stale_exits > 0:
            logger.info(f"")
            logger.info(f"   ─── STALE EXIT ANALYSIS ───")
            logger.info(f"   Stale exits:       {self.stale_exits:>10}")
            logger.info(f"   Saved vs max loss: ${self.stale_saved:>+10,.0f}")
            stale_trades = [t for t in all_trades if 'Stale' in (t.exit_reason or '')]
            stale_pnl = sum(t.lifecycle_pnl for t in stale_trades)
            logger.info(f"   Stale exit P&L:    ${stale_pnl:>+10,.0f}")
            exp_trades = [t for t in all_trades if t.exit_reason == 'Expiration']
            logger.info(f"   Expiration exits:  {len(exp_trades):>10}")
        # Days held analysis
        logger.info(f"")
        logger.info(f"   ─── DAYS HELD ANALYSIS ───")
        days_winners = []
        days_losers = []
        for t in non_force:
            try:
                ed = datetime.strptime(t.entry_date, '%Y-%m-%d').date()
                xd = datetime.strptime(t.exit_date, '%Y-%m-%d').date()
                biz = sum(1 for i in range((xd - ed).days + 1) if (ed + timedelta(days=i)).weekday() < 5) - 1
                biz = max(biz, 0)
                if t.lifecycle_pnl > 0:
                    days_winners.append(biz)
                else:
                    days_losers.append(biz)
            except:
                pass
        if days_winners:
            from collections import Counter
            w_dist = Counter(days_winners)
            logger.info(f"   Winners ({len(days_winners)} trades):")
            for d in sorted(w_dist.keys()):
                logger.info(f"      Day {d}: {w_dist[d]} trades ({w_dist[d]/len(days_winners):.0%})")
            logger.info(f"      Avg: {sum(days_winners)/len(days_winners):.1f} days")
        if days_losers:
            from collections import Counter
            l_dist = Counter(days_losers)
            logger.info(f"   Losers ({len(days_losers)} trades):")
            for d in sorted(l_dist.keys()):
                logger.info(f"      Day {d}: {l_dist[d]} trades ({l_dist[d]/len(days_losers):.0%})")
            logger.info(f"      Avg: {sum(days_losers)/len(days_losers):.1f} days")
        if force_closed:
            logger.info(f"")
            logger.info(f"   ─── FORCE-CLOSED DETAIL (included in stats above) ───")
            for t in force_closed:
                logger.info(f"      {t.symbol}: ${t.lifecycle_pnl:+,.0f} (unrealized at replay end, MAE={t.mae:.0%})")

        # v3.3.1: Rolling stats
        if self.rolling_enabled:
            logger.info(f"")
            logger.info(f"   ─── ROLLING STATS (v3.3.2 — corrected accounting) ───")
            logger.info(f"   Attempted:         {self.roll_stats['attempted']:>10}")
            logger.info(f"   Completed:         {self.roll_stats['completed']:>10}")
            logger.info(f"   Skipped:           {self.roll_stats['skipped']:>10}")
            logger.info(f"   Net credit from rolls: ${self.roll_stats['total_net_credit']:>+10,.0f}")
            logger.info(f"   ⚠️  Roll costs applied to balance: YES (v3.3.2 fix)")
            logger.info(f"   Rolled trades:     {len(rolled_trades):>10}")
            if rolled_trades:
                rolled_winners = [t for t in rolled_trades if t.lifecycle_pnl > 0]
                rolled_losers = [t for t in rolled_trades if t.lifecycle_pnl <= 0]
                logger.info(f"     → Winners after rolling: {len(rolled_winners)}")
                logger.info(f"     → Losers after rolling:  {len(rolled_losers)}")
                logger.info(f"     → Lifecycle P&L (rolled): ${sum(t.lifecycle_pnl for t in rolled_trades):>+,.0f}")
            # Show which positions were rolled
            rolled_positions = [t for t in all_trades if t.roll_count > 0]
            if rolled_positions:
                logger.info(f"   Rolled positions:")
                for t in rolled_positions:
                    strategy = 'BPS' if t.direction == 'bullish' else 'BCS'
                    for r in t.roll_history:
                        logger.info(f"      {t.symbol} {strategy}: {r['old_exp']} → {r['new_exp']} "
                                   f"| Close ${r['close_cost']:.2f} | New ${r['new_credit']:.2f} "
                                   f"| Net ${r['net_credit']:+.2f} "
                                   f"| {r['old_strikes']} → {r['new_strikes']}")
                    logger.info(f"      → Final spread P&L: ${t.pnl:+,.0f} | Roll costs: ${t.roll_cost_total:+,.0f} | Lifecycle P&L: ${t.lifecycle_pnl:+,.0f} (rolled {t.roll_count}x)")

        # v3.3.1: Credit stop stats
        if self.credit_stop_multiplier > 0:
            logger.info(f"")
            logger.info(f"   ─── CREDIT STOP STATS (v3.3.1) ───")
            logger.info(f"   Stop triggers:     {self.stop_stats['triggered']:>10}")
            logger.info(f"   Saved vs max loss: ${self.stop_stats['total_saved_vs_max']:>+10,.0f}")
            stop_trades = [t for t in all_trades if 'Credit stop' in (t.exit_reason or '')]
            if stop_trades:
                for t in stop_trades:
                    strategy = 'BPS' if t.direction == 'bullish' else 'BCS'
                    logger.info(f"      {t.symbol} {strategy} {t.short_strike}/{t.long_strike}: "
                               f"${t.lifecycle_pnl:+,.0f} ({t.exit_reason})")

        # v3.4.9: Conditional IC stats
        if self.conditional_ic:
            logger.info(f"")
            logger.info(f"   ─── CONDITIONAL IRON CONDOR STATS (v3.4.9) ───")
            logger.info(f"   Trigger type:      {self.ic_trigger}")
            logger.info(f"   DTE mode:          {self.ic_dte_mode}")
            logger.info(f"   Triggers checked:  {self.ic_stats['checked']:>10}")
            logger.info(f"   Triggers fired:    {self.ic_stats['triggered']:>10}")
            logger.info(f"   IC built:          {self.ic_stats['built']:>10}")
            logger.info(f"   IC failed:         {self.ic_stats['failed']:>10}")
            ic_positions = [t for t in all_trades if t.has_opposite_leg]
            non_ic_positions = [t for t in all_trades if t.is_conditional_ic and not t.has_opposite_leg]
            logger.info(f"   Converted to IC:   {len(ic_positions):>10}")
            logger.info(f"   Stayed directional:{len(non_ic_positions):>10}")
            if ic_positions:
                ic_pnl = sum(t.lifecycle_pnl for t in ic_positions)
                ic_winners = sum(1 for t in ic_positions if t.lifecycle_pnl > 0)
                logger.info(f"   IC P&L:            ${ic_pnl:>+10,.0f}")
                logger.info(f"   IC win rate:       {ic_winners/len(ic_positions)*100:>10.1f}%")
                avg_opp_credit = sum(t.opposite_leg_credit for t in ic_positions) / len(ic_positions)
                logger.info(f"   Avg opp credit:    ${avg_opp_credit:>10.2f}")

        # v3.4.1: ADVISOR STRESS TEST VERDICT
        logger.info(f"")
        logger.info(f"   ─── ADVISOR STRESS TEST ───")
        logger.info(f"   Execution model:   {self.execution_model.upper()}")
        logger.info(f"   Profit Factor:     {profit_factor:.2f}")
        if self.commissions_enabled:
            logger.info(f"   Commissions:       ${self.total_commissions:,.0f}")
        if profit_factor >= 2.0:
            logger.info(f"   ✅ PF {profit_factor:.2f} ≥ 2.0 → LIVE VIABLE")
        elif profit_factor >= 1.5:
            logger.info(f"   ⚠️ PF {profit_factor:.2f} — borderline (target: 2.0)")
        else:
            logger.info(f"   ❌ PF {profit_factor:.2f} < 2.0 → NEEDS WORK before live")
        net_return = self.balance - self.starting_capital
        if net_return > 0:
            logger.info(f"   ✅ Strategy profitable: ${net_return:+,.0f} ({net_return/self.starting_capital:+.1%})")
        else:
            logger.info(f"   ❌ Strategy unprofitable: ${net_return:+,.0f} ({net_return/self.starting_capital:+.1%})")

        # Trade log
        logger.info(f"\n   📋 TRADE LOG (all trades):")
        for t in all_trades:
            strategy = 'BPS' if t.direction == 'bullish' else 'BCS'
            fc_flag = ' [FC]' if t.status == 'force_closed' else ''
            tp_time = f" @{t.tp_bar_time}" if t.tp_bar_time else ''
            roll_flag = f' [R{t.roll_count}]' if t.roll_count > 0 else ''
            ic_flag = f' [IC +${t.opposite_leg_credit:.2f}]' if t.has_opposite_leg else ''
            logger.info(f"      {t.entry_date} {t.signal_time} | {t.symbol:>6} {strategy} "
                       f"{t.short_strike}/{t.long_strike} x{t.quantity} "
                       f"@ ${t.credit:.2f} (slip=${t.slippage_applied:.2f}) "
                       f"→ {t.exit_reason}: ${t.lifecycle_pnl:+,.0f} "
                       f"(MAE={t.mae:.0%}, bars={t.polygon_bar_count}){fc_flag}{roll_flag}{ic_flag}")

    def save_results(self):
        output_dir = os.path.dirname(os.path.abspath(__file__))

        trades_file = os.path.join(output_dir, 'replay_trades_v341.csv')
        with open(trades_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Entry Date', 'Signal Time', 'Symbol', 'Direction', 'Strategy',
                'Short Strike', 'Long Strike', 'Spread Width',
                'Raw Credit', 'Slippage', 'Net Credit', 'Credit Ratio',
                'Quantity', 'Expiration', 'DTE',
                'Spot at Entry', 'Conviction', 'Weighted Premium',
                'Exit Date', 'Exit Reason', 'Spread P&L', 'Roll Cost', 'Lifecycle P&L', 'Status',
                'MAE', 'MAE Date', 'TP Time',
                'Polygon Bars', 'Short OCC', 'Long OCC', 'Roll Count',
                'IC Converted', 'IC Trigger Type', 'IC Trigger Date', 'Opp Credit', 'Total Credit',
            ])
            for t in self.closed_positions:
                raw_credit = t.credit + t.slippage_applied
                writer.writerow([
                    t.entry_date, t.signal_time, t.symbol, t.direction,
                    'Bull Put Spread' if t.direction == 'bullish' else 'Bear Call Spread',
                    t.short_strike, t.long_strike, t.spread_width,
                    f"{raw_credit:.2f}", f"{t.slippage_applied:.2f}",
                    f"{t.credit:.2f}", f"{t.credit/t.spread_width:.2%}",
                    t.quantity, t.expiration, t.dte_at_entry,
                    f"{t.spot_at_entry:.2f}", f"{t.conviction:.2%}",
                    f"{t.weighted_premium:.0f}",
                    t.exit_date, t.exit_reason, f"{t.pnl:.2f}",
                    f"{t.roll_cost_total:.2f}", f"{t.lifecycle_pnl:.2f}", t.status,
                    f"{t.mae:.2%}", t.mae_date or '',
                    t.tp_bar_time or '',
                    t.polygon_bar_count,
                    t.short_occ, t.long_occ, t.roll_count,
                    t.has_opposite_leg, t.ic_trigger_type or '',
                    t.ic_trigger_date or '', f"{t.opposite_leg_credit:.2f}",
                    f"{t.total_credit:.2f}",
                ])
        logger.info(f"\n   💾 Trade log: {trades_file}")

        equity_file = os.path.join(output_dir, 'replay_equity_v341.csv')
        with open(equity_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Date', 'Balance', 'Unrealized', 'Total Equity', 'Open Positions'])
            for eq in self.equity_curve:
                writer.writerow([eq['date'], f"{eq['balance']:.2f}",
                                f"{eq['unrealized']:.2f}", f"{eq['total_equity']:.2f}",
                                eq['open_positions']])
        logger.info(f"   💾 Equity curve: {equity_file}")

        signals_file = os.path.join(output_dir, 'replay_signals_v341.csv')
        with open(signals_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Date', 'Signal Time', 'Symbol', 'Direction', 'Conviction',
                'Weighted Premium', 'Spot', 'Short Strike', 'Long Strike',
                'Expiration', 'Raw Credit', 'Slippage', 'Net Credit',
                'Spread Width', 'Credit Ratio', 'Quantity', 'Polygon Bars'
            ])
            for s in self.all_signals:
                writer.writerow([
                    s['date'], s['signal_time'], s['symbol'], s['direction'],
                    f"{s['conviction']:.2%}", f"{s['weighted_premium']:.0f}",
                    f"{s['spot']:.2f}", s['short_strike'], s['long_strike'],
                    s['expiration'], f"{s['raw_credit']:.2f}",
                    f"{s['slippage']:.2f}", f"{s['credit']:.2f}",
                    s['spread_width'], f"{s['credit_ratio']:.2%}", s['quantity'],
                    s.get('bar_count', 0),
                ])
        logger.info(f"   💾 Signals: {signals_file}")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Replay Engine v3.4.5 — Real Bid/Ask Execution Model')
    parser.add_argument('--data', '-d', required=True, nargs='+', help='Path(s) to UW flow CSV(s)')
    parser.add_argument('--capital', type=float, default=STARTING_CAPITAL)
    parser.add_argument('--slippage', type=float, default=DEFAULT_SLIPPAGE_PER_LEG,
                        help=f'Slippage per leg in dollars (default: ${DEFAULT_SLIPPAGE_PER_LEG})')
    parser.add_argument('--scan-interval', type=int, default=DEFAULT_SCAN_INTERVAL,
                        help=f'Scan interval in minutes (default: {DEFAULT_SCAN_INTERVAL})')
    parser.add_argument('--bar-size', type=int, default=DEFAULT_BAR_SIZE_MIN,
                        help=f'Polygon bar size in minutes (default: {DEFAULT_BAR_SIZE_MIN})')
    parser.add_argument('--no-blacklist', action='store_true')
    parser.add_argument('--max-hold-days', type=int, default=0,
                        help='Exit after N trading days if no TP hit (0=disabled)')
    parser.add_argument('--stale-rolled-only', action='store_true',
                        help='Only apply max-hold-days to ROLLED positions (non-rolled keep running)')
    parser.add_argument('--max-credit-ratio', type=float, default=MAX_CREDIT_RATIO,
                        help='Max credit/width ratio at entry (0.35=aggressive filter, 1.0=disabled)')
    parser.add_argument('--no-rolling', action='store_true',
                        help='Disable rolling logic (for A/B comparison)')
    parser.add_argument('--holiday-blackout', nargs='?', const='clusters', default=False,
                        choices=['clusters', 'all'],
                        help='Holiday blackout mode: "clusters" (default, recommended) blocks only '
                             'Thanksgiving/Christmas/NYE; "all" blocks every market holiday including '
                             'single-day holidays like MLK, Presidents Day, etc.')
    parser.add_argument('--credit-stop', type=float, default=0.0,
                        help='Credit stop multiplier (e.g. 2.5 = stop at 2.5x credit, 0=disabled)')
    # v3.4 arguments
    parser.add_argument('--execution-model', choices=['fixed', 'calibrated'], default='fixed',
                        help='Execution model: fixed=$0.05/leg slippage, calibrated=iVol half-spread slippage')
    parser.add_argument('--commissions', action='store_true',
                        help=f'Enable commission tracking (${COMMISSION_PER_LEG}/leg/contract)')
    parser.add_argument('--tp-mode', choices=['fixed', 'trail'], default='fixed',
                        help='TP mode: fixed or trail')
    parser.add_argument('--trail-activate', type=float, default=TRAIL_ACTIVATION_PCT,
                        help='Trail activation pct')
    parser.add_argument('--trail-retrace', type=float, default=TRAIL_RETRACE_PCT,
                        help='Trail retrace pct')
    parser.add_argument('--max-positions', type=int, default=MAX_POSITIONS,
                        help=f'Maximum simultaneous open positions (default: {MAX_POSITIONS})')
    parser.add_argument('--ivol-key', type=str, default=IVOL_API_KEY,
                        help='iVolatility API key (default: from env or config)')
    # v3.4.7: Optimization sweep arguments
    parser.add_argument('--min-credit-ratio', type=float, default=MIN_CREDIT_RATIO,
                        help=f'Minimum credit/width ratio (default: {MIN_CREDIT_RATIO})')
    parser.add_argument('--roll-max-dte', type=int, default=ROLL_MAX_DTE,
                        help=f'Roll when DTE <= this value (default: {ROLL_MAX_DTE})')
    parser.add_argument('--market-bias-block', type=float, default=0.0,
                        help='Block BPS when market bear%% > threshold (0=disabled, 0.65=65%%)')
    parser.add_argument('--vix-block-bps', type=float, default=0,
                        help='Block BPS entries above this VIX level (0=disabled)')
    parser.add_argument('--vix-unblock-bps', type=float, default=0,
                        help='Re-enable BPS below this VIX level (0=disabled)')
    parser.add_argument('--extra-whitelist', type=str, default='',
                        help='Comma-separated tickers to add to watchlist')
    parser.add_argument('--remove-blacklist', type=str, default='',
                        help='Comma-separated tickers to remove from blacklist')
    parser.add_argument('--bcs-preference', type=float, default=0.0,
                        help='Force BCS when market bear%% > threshold (0=disabled, 0.60=60%%)')
    parser.add_argument('--position-size', type=float, default=MAX_POSITION_SIZE_PCT,
                        help=f'Position size as fraction of balance (default: {MAX_POSITION_SIZE_PCT})')
    parser.add_argument('--take-profit', type=float, default=TAKE_PROFIT_PCT,
                        help=f'Take profit percentage of credit (default: {TAKE_PROFIT_PCT})')
    parser.add_argument('--put-only', action='store_true',
                        help='Put-only mode: skip all bearish BCS signals, only trade Bull Put Spreads')
    parser.add_argument('--min-dte', type=int, default=MIN_DTE,
                        help=f'Minimum DTE for entries (default: {MIN_DTE})')
    parser.add_argument('--max-dte', type=int, default=MAX_DTE,
                        help=f'Maximum DTE for entries (default: {MAX_DTE})')
    parser.add_argument('--profit-recapture', action='store_true',
                        help='Profit recapture exit: close position if it was profitable then goes negative')
    # v3.4.9: Conditional Iron Condor arguments
    parser.add_argument('--conditional-ic', action='store_true',
                        help='Enable conditional iron condor mode: start directional, add opposite leg on trigger')
    parser.add_argument('--ic-trigger', choices=['flow_reversal', 'price_move', 'spread_sl'],
                        default='flow_reversal',
                        help='Trigger type for adding opposite IC leg (default: flow_reversal)')
    parser.add_argument('--ic-dte-mode', choices=['same_exp', 'fresh_dte'], default='same_exp',
                        help='DTE mode for opposite leg: same_exp or fresh_dte (default: same_exp)')
    parser.add_argument('--ic-price-move-pct', type=float, default=3.0,
                        help='Price move threshold %% for price_move trigger (default: 3.0)')
    parser.add_argument('--ic-spread-sl-mult', type=float, default=1.5,
                        help='Spread SL multiplier for spread_sl trigger (default: 1.5)')
    parser.add_argument('--ic-dte-min', type=int, default=30,
                        help='Min DTE for conditional IC entries (default: 30)')
    parser.add_argument('--ic-dte-max', type=int, default=45,
                        help='Max DTE for conditional IC entries (default: 45)')

    args = parser.parse_args()

    # v3.4.9: Override DTE range for conditional IC mode
    if args.conditional_ic:
        if args.min_dte == MIN_DTE:  # Not explicitly set by user
            args.min_dte = args.ic_dte_min
        if args.max_dte == MAX_DTE:  # Not explicitly set by user
            args.max_dte = args.ic_dte_max
        logger.info(f"   🔄 Conditional IC mode: DTE range overridden to {args.min_dte}-{args.max_dte}")

    if args.no_blacklist:
        TICKER_BLACKLIST.clear()

    # Apply extra whitelist / remove blacklist
    if args.extra_whitelist:
        for ticker in args.extra_whitelist.split(','):
            t = ticker.strip().upper()
            if t:
                WATCHLIST.add(t)
        logger.info(f"   Added to whitelist: {args.extra_whitelist}")
    if args.remove_blacklist:
        for ticker in args.remove_blacklist.split(','):
            t = ticker.strip().upper()
            TICKER_BLACKLIST.discard(t)
        logger.info(f"   Removed from blacklist: {args.remove_blacklist}")

    for f in args.data:
        if not os.path.exists(f):
            logger.error(f"❌ Data file not found: {f}")
            sys.exit(1)

    # Initialize Tradier (for strikes)
    logger.info(f"🔗 Initializing Tradier API client (strikes)...")
    tradier = TradierClient(TRADIER_API_KEY)
    test = tradier._request('markets/quotes', {'symbols': 'SPY'})
    if test:
        logger.info(f"   ✅ Tradier API connected")
    else:
        logger.error(f"   ❌ Tradier API connection failed")
        sys.exit(1)

    # Initialize Polygon (for option bars)
    logger.info(f"🔗 Initializing Polygon/Massive API client ({args.bar_size}-min bars)...")
    polygon = PolygonClient(POLYGON_API_KEY)
    # Quick connectivity test with a known liquid option
    test_bars = polygon.get_option_bars('SPY251219C00650000', '2025-12-01', '2025-12-05',
                                         multiplier=1, timespan='day')
    if test_bars is not None:
        logger.info(f"   ✅ Polygon API connected (test returned {len(test_bars or [])} bars)")
    else:
        logger.error(f"   ❌ Polygon API connection failed — check API key")
        sys.exit(1)

    # v3.4.1: Initialize iVol client if calibrated mode
    ivol_client = None
    if args.execution_model == 'calibrated':
        logger.info(f"🔗 Initializing iVolatility API client (bid/ask execution)...")
        ivol_client = IVolClient(api_key=args.ivol_key)
        logger.info(f"   ✅ iVol client ready (rate limit: {IVOL_RATE_LIMIT}s between calls)")
        logger.info(f"   ⚠️ NOTE: ~4 iVol calls per trade event. Estimated runtime:")
        logger.info(f"      95 trades × ~6 events × {IVOL_RATE_LIMIT}s ≈ 10-12 minutes")

    engine = ReplayEngine(
        tradier, polygon, args.capital,
        slippage_per_leg=args.slippage,
        scan_interval_min=args.scan_interval,
        bar_size_min=args.bar_size,
        max_hold_days=args.max_hold_days,
        rolling_enabled=not args.no_rolling,
        credit_stop_multiplier=args.credit_stop,
        execution_model=args.execution_model,          # v3.4
        commissions_enabled=args.commissions,           # v3.4
        ivol_client=ivol_client,                        # v3.4
        tp_mode=args.tp_mode,
        trail_activate_pct=args.trail_activate,
        trail_retrace_pct=args.trail_retrace,
        stale_rolled_only=args.stale_rolled_only,
        max_credit_ratio=args.max_credit_ratio,
        holiday_blackout=args.holiday_blackout,
        max_positions=args.max_positions,
        position_size_pct=args.position_size,
        min_credit_ratio=args.min_credit_ratio,
        roll_max_dte=args.roll_max_dte,
        market_bias_block=args.market_bias_block,
        vix_block_bps=args.vix_block_bps,
        vix_unblock_bps=args.vix_unblock_bps,
        bcs_preference_threshold=args.bcs_preference,
        take_profit_pct=args.take_profit,
        put_only=args.put_only,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        profit_recapture=args.profit_recapture,
        conditional_ic=args.conditional_ic,
        ic_trigger=args.ic_trigger,
        ic_dte_mode=args.ic_dte_mode,
        ic_price_move_pct=args.ic_price_move_pct,
        ic_spread_sl_mult=args.ic_spread_sl_mult,
        ic_dte_min=args.ic_dte_min,
        ic_dte_max=args.ic_dte_max,
    )
    engine.run(args.data)
    logger.info(f"\n✅ Replay complete!")
    logger.info(f"   Polygon API calls: {polygon.api_calls}")
    logger.info(f"   Tradier API calls: {tradier.api_calls}")
    dc = get_disk_cache()
    logger.info(f"   Disk cache: {dc.stats()}")
    dc.close()


if __name__ == '__main__':
    main()