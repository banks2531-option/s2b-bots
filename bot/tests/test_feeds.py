from bot.app.feeds import pick_weekly_expiry


def test_pick_weekly_expiry_next_friday_min_dte():
    # Monday 2026-06-15: the Friday 2026-06-19 is 4 DTE -> ok
    assert pick_weekly_expiry("2026-06-15", min_dte=4) == "2026-06-19"


def test_pick_weekly_expiry_skips_too_near_friday():
    # Thursday 2026-06-18: that-week Friday (06-19) is only 1 DTE -> roll to next Friday 06-26
    assert pick_weekly_expiry("2026-06-18", min_dte=4) == "2026-06-26"


from bot.app.feeds import parse_chain
from bot.strategy.s2b import OptionQuote

CHAIN_RESP = {"options": {"option": [
    {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
    {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
    {"strike": 580.0, "option_type": "call", "bid": 2.00, "ask": 2.10, "greeks": {"delta": 0.40}},
    {"strike": 560.0, "option_type": "put", "bid": 2.00, "ask": 2.10, "greeks": {"delta": None}},
]}}


def test_parse_chain_puts_only_abs_delta():
    quotes = parse_chain(CHAIN_RESP)
    # calls excluded; the null-delta put excluded; two clean puts remain with ABS delta
    assert quotes == [OptionQuote(568.0, 0.36, 3.40, 3.50), OptionQuote(558.0, 0.18, 1.60, 1.70)]


def test_parse_chain_single_option_dict():
    single_resp = {"options": {"option":
        {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}}
    }}
    result = parse_chain(single_resp)
    assert len(result) == 1
    assert result[0].strike == 568.0


def test_parse_chain_skips_missing_bid():
    resp = {"options": {"option": [
        {"strike": 568.0, "option_type": "put", "ask": 3.50, "greeks": {"delta": -0.36}},  # no bid
        {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
    ]}}
    result = parse_chain(resp)
    assert len(result) == 1
    assert result[0].strike == 558.0


from bot.app.feeds import parse_equity, parse_position_legs


def test_parse_equity():
    assert parse_equity({"balances": {"total_equity": 20123.45}}) == 20123.45


def test_parse_position_legs_maps_symbol_to_qty():
    resp = {"positions": {"position": [
        {"symbol": "SPY260619P00568000", "quantity": -2.0},
        {"symbol": "SPY260619P00558000", "quantity": 2.0},
    ]}}
    assert parse_position_legs(resp) == {"SPY260619P00568000": -2, "SPY260619P00558000": 2}


def test_parse_position_legs_handles_no_positions():
    assert parse_position_legs({"positions": "null"}) == {}
    # Tradier returns a single object (not a list) when exactly one position exists
    assert parse_position_legs({"positions": {"position": {"symbol": "X", "quantity": 1.0}}}) == {"X": 1}


from bot.app.feeds import compute_atr, vix_regime


def test_compute_atr_simple():
    # 3 bars, each true range = high-low = 2.0 (no gaps) -> ATR(2) = 2.0
    bars = [{"high": 10, "low": 8, "close": 9},
            {"high": 11, "low": 9, "close": 10},
            {"high": 12, "low": 10, "close": 11}]
    assert compute_atr(bars, n=2) == 2.0


def test_vix_regime_pct_rank_and_change():
    series = [10, 12, 14, 16, 20]   # latest 20 is the max -> pct_rank 1.0; change 20/16-1 = 0.25
    pct_rank, change = vix_regime(series)
    assert pct_rank == 1.0
    assert round(change, 4) == 0.25


import pytest

def test_compute_atr_empty_raises():
    with pytest.raises(ValueError):
        compute_atr([])


from bot.app.feeds import parse_occ, reconstruct_spreads

def test_parse_occ_roundtrip():
    assert parse_occ("SPY260619P00568000") == ("SPY", "2026-06-19", "P", 568.0)

def test_reconstruct_spreads_pairs_bull_put():
    from bot.strategy.manage import ManagedPosition
    result = reconstruct_spreads({"SPY260619P00568000": -2, "SPY260619P00558000": 2})
    assert len(result) == 1
    p = result[0]
    assert p.ticker == "SPY"
    assert p.short_strike == 568.0
    assert p.long_strike == 558.0
    assert p.qty == 2
    assert p.expiry == "2026-06-19"
