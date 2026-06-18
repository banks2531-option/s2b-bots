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
