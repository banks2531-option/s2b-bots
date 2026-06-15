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


def test_place_order_returns_id():
    http = make_http({("POST", "/accounts/ABC/orders"): {"order": {"id": 123456, "status": "ok"}}})
    c = TradierClient(account_id="ABC", http=http)
    oid = c.place_order({"class": "multileg", "symbol": "SPY"})
    assert oid == "123456"
    assert http.calls[0][3] == {"class": "multileg", "symbol": "SPY"}  # payload forwarded


def test_place_order_missing_id_raises():
    # C6: an order with no parseable id is a FAILED submission, not a silent success
    http = make_http({("POST", "/accounts/ABC/orders"): {"errors": {"error": ["bad order"]}}})
    c = TradierClient(account_id="ABC", http=http)
    with pytest.raises(BrokerError):
        c.place_order({"class": "multileg"})


def test_get_order_returns_order_dict():
    http = make_http({("GET", "/accounts/ABC/orders/123456"):
                      {"order": {"id": 123456, "status": "filled", "avg_fill_price": 3.05}}})
    c = TradierClient(account_id="ABC", http=http)
    o = c.get_order("123456")
    assert o["status"] == "filled" and o["avg_fill_price"] == 3.05


def test_cancel_order_calls_delete():
    http = make_http({("DELETE", "/accounts/ABC/orders/123456"): {"order": {"id": 123456, "status": "ok"}}})
    c = TradierClient(account_id="ABC", http=http)
    c.cancel_order("123456")
    assert http.calls[0][0] == "DELETE"
