"""Tradier REST adapter (spec §7). HTTP transport is injected for hermetic tests."""
from dataclasses import dataclass


class BrokerError(Exception):
    pass


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
        return Quote(symbol=q["symbol"], bid=float(q["bid"]),
                     ask=float(q["ask"]), last=float(q["last"]))

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
        self.http("DELETE", f"/accounts/{self.account_id}/orders/{order_id}")
