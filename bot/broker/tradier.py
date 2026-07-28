"""Tradier REST adapter (spec §7). HTTP transport is injected for hermetic tests."""
import time as _time
from dataclasses import dataclass

from bot.errors import MarketDataUnavailable

# Transient network exceptions that a GET may be retried on. Resolved lazily so importing this module
# never requires `requests` (kept out of the import path for hermetic unit tests); production has it.
try:
    import requests as _rq
    _TRANSIENT = (_rq.exceptions.Timeout, _rq.exceptions.ConnectionError)
except Exception:                       # pragma: no cover - requests always present in production
    _TRANSIENT = ()


class BrokerError(Exception):
    pass


def _get_with_retry(request_fn, *, attempts=4, base_delay=0.5, sleep_fn=_time.sleep, transient=None):
    """Call an IDEMPOTENT GET (request_fn() -> a response with .status_code/.raise_for_status()/.json())
    with exponential backoff on transient failures -- connection/read timeouts and 5xx server errors --
    then raise MarketDataUnavailable. 4xx still raises HTTPError immediately (a real client error, not a
    blip). Used ONLY for GETs: a POST/DELETE that times out may have executed at the broker, so those
    are NEVER retried (see http() below)."""
    transient = _TRANSIENT if transient is None else transient
    delay, last = base_delay, None
    for i in range(attempts):
        try:
            r = request_fn()
            if getattr(r, "status_code", 200) >= 500:
                last = BrokerError("%s server error" % r.status_code)
            else:
                r.raise_for_status()
                return r.json()
        except transient as exc:
            last = exc
        if i < attempts - 1:
            sleep_fn(delay)
            delay *= 2
    raise MarketDataUnavailable("tradier GET unavailable after %d attempts: %s" % (attempts, last)) from last


def build_http(request_fn, base, sleep_fn=_time.sleep):
    """Build an http(method, path, params, data) callable from a raw request_fn(method, url, params,
    data) -> response. GETs get retry/backoff (idempotent market data); every other verb (order
    submit/cancel) is issued EXACTLY ONCE and behaves as before -- a non-idempotent call must never be
    auto-retried."""
    def http(method, path, params=None, data=None):
        url = base + path
        if method != "GET":
            r = request_fn(method, url, params, data)
            # 2026-07-27 diagnostics: on an order (POST/DELETE) rejection, surface the broker's actual
            # error BODY -- raise_for_status() alone gives only "400 Client Error:" with no reason, which
            # is exactly what left the 7/27 close-rejection root cause opaque. The body carries Tradier's
            # message (e.g. an invalid-price explanation); it never contains credentials (those are
            # headers). Truncated so a large body can't flood the log.
            if getattr(r, "status_code", 200) >= 400:
                body = ""
                try:
                    body = (r.text or "")[:500]
                except Exception:
                    body = "<unreadable body>"
                raise BrokerError("%s %s -> HTTP %s: %s" % (method, path, r.status_code, body))
            return r.json()
        return _get_with_retry(lambda: request_fn("GET", url, params, None), sleep_fn=sleep_fn)
    return http


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

    def request_fn(method, url, params, data):
        return session.request(method, url, params=params, data=data, timeout=30)

    # GET retry/backoff (market-data resilience); orders (POST/DELETE) issued exactly once.
    return build_http(request_fn, base)
