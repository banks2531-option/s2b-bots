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
