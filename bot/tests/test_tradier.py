import pytest
from bot.broker.tradier import TradierClient, Quote, BrokerError


def make_http(responses):
    """responses: dict keyed by (method, path) -> json dict. Records calls."""
    calls = []

    def http(method, path, params=None, data=None):
        calls.append((method, path, params, data))
        return responses[(method, path)]

    http.calls = calls
    return http


def test_get_quote_parses_fields():
    http = make_http({
        ("GET", "/markets/quotes"): {"quotes": {"quote": {
            "symbol": "SPY", "bid": 574.50, "ask": 574.60, "last": 574.55}}}
    })
    c = TradierClient(account_id="ABC", http=http)
    q = c.get_quote("SPY")
    assert q == Quote(symbol="SPY", bid=574.50, ask=574.60, last=574.55)
    # verify it asked for the right symbol
    assert http.calls[0][2] == {"symbols": "SPY"}
