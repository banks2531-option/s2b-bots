"""Tradier REST adapter (spec §7). HTTP transport is injected for hermetic tests."""
from dataclasses import dataclass


class BrokerError(Exception):
    pass


def _to_float(v, field):
    if v is None:
        raise BrokerError(f"quote field '{field}' is null")
    return float(v)


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float


class TradierClient:
    def __init__(self, account_id, http):
        """http: callable(method, path, params=None, data=None) -> parsed-json dict."""
        self.account_id = account_id
        self.http = http

    def get_quote(self, symbol: str) -> Quote:
        resp = self.http("GET", "/markets/quotes", params={"symbols": symbol})
        q = resp.get("quotes", {}).get("quote")
        if not q:
            raise BrokerError(f"no quote for {symbol}")
        return Quote(symbol=q["symbol"], bid=_to_float(q.get("bid"), "bid"),
                     ask=_to_float(q.get("ask"), "ask"), last=_to_float(q.get("last"), "last"))

    def place_order(self, payload: dict) -> str:
        """Submit an order; return broker order id as str. Raise if no id (C6: no silent fail)."""
        resp = self.http("POST", f"/accounts/{self.account_id}/orders", data=payload)
        oid = resp.get("order", {}).get("id")
        if oid is None:
            raise BrokerError(f"order submission returned no id: {resp}")
        return str(oid)

    def get_order(self, order_id: str) -> dict:
        resp = self.http("GET", f"/accounts/{self.account_id}/orders/{order_id}")
        order = resp.get("order")
        if not order:
            raise BrokerError(f"no order {order_id}")
        return order

    def cancel_order(self, order_id: str) -> None:
        resp = self.http("DELETE", f"/accounts/{self.account_id}/orders/{order_id}")
        if isinstance(resp, dict) and "errors" in resp:
            raise BrokerError(f"cancel failed for {order_id}: {resp['errors']}")


def make_http_from_env():
    """Build a real requests-backed http() callable. Credentials from env only (C8)."""
    import os
    import requests  # imported here so unit tests don't require the dep at import time

    token = os.environ.get("TRADIER_TOKEN")
    base = os.environ.get("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
    if not token:
        raise BrokerError("TRADIER_TOKEN not set (C8: credentials from environment only)")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def http(method, path, params=None, data=None):
        r = session.request(method, base + path, params=params, data=data, timeout=30)
        r.raise_for_status()
        return r.json()

    return http
