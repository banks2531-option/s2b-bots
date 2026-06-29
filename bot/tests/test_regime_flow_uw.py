# bot/tests/test_regime_flow_uw.py
from bot.regime.flow_uw import parse_market_tide, flow_context


def test_parse_market_tide_bullish_bearish():
    # Real UW market-tide shape: premiums are STRINGS; net_put_premium is signed (negative = puts
    # SOLD = bullish). Sentiment = net_call_premium - net_put_premium; >0 bullish, <0 bearish.
    # bullish tape: calls bought (+) AND puts sold (-)
    assert parse_market_tide({"data": [{"net_call_premium": "3e8", "net_put_premium": "-1e8"}]}) == ("bullish", False)
    # bearish tape: calls sold (-) AND puts bought (+)
    assert parse_market_tide({"data": [{"net_call_premium": "-2e8", "net_put_premium": "3e8"}]}) == ("bearish", False)
    # blow-out bearish flow (|sentiment| >= $1B) -> extreme risk-off veto
    bias, extreme = parse_market_tide({"data": [{"net_call_premium": "-1e9", "net_put_premium": "5e8"}]})
    assert bias == "bearish" and extreme is True
    # uses the most recent (last) bucket
    assert parse_market_tide({"data": [
        {"net_call_premium": "1e9", "net_put_premium": "-1e9"},
        {"net_call_premium": "-3e8", "net_put_premium": "1e8"}]}) == ("bearish", False)


def test_flow_context_degrades_gracefully_on_error():
    def boom(*a, **k):
        raise ConnectionError("UW down")
    # any failure -> neutral/False, never raises (Phase 0 must not break the bot)
    assert flow_context(http=boom) == ("neutral", False)
