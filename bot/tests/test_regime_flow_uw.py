# bot/tests/test_regime_flow_uw.py
from bot.regime.flow_uw import parse_market_tide, flow_context


def test_parse_market_tide_bullish_bearish():
    # net call premium dominating -> bullish; net put premium dominating -> bearish
    assert parse_market_tide({"data": [{"net_call_premium": 9e8, "net_put_premium": 1e8}]}) == ("bullish", False)
    assert parse_market_tide({"data": [{"net_call_premium": 1e8, "net_put_premium": 9e8}]}) == ("bearish", False)
    # extreme one-sided put premium -> bearish AND extreme veto
    bias, extreme = parse_market_tide({"data": [{"net_call_premium": 1e7, "net_put_premium": 2e9}]})
    assert bias == "bearish" and extreme is True


def test_flow_context_degrades_gracefully_on_error():
    def boom(*a, **k):
        raise ConnectionError("UW down")
    # any failure -> neutral/False, never raises (Phase 0 must not break the bot)
    assert flow_context(http=boom) == ("neutral", False)
