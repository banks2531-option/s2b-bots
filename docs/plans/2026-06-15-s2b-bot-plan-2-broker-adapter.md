# S2b Bot — Plan 2: Tradier Broker Adapter + Order State Machine (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Tradier REST adapter (`get_quote`, `place_order`, `get_order`, `cancel_order`) plus an order state machine that submits an order, verifies the fill, and cancels-on-timeout — with broker-order-id verification (C6). All hermetically testable.

**Architecture:** `bot/broker/tradier.py` holds `TradierClient`, which takes an **injected `http` callable** (`http(method, path, params=None, data=None) -> dict`) so unit tests pass a fake transport returning recorded Tradier JSON — zero network. `bot/broker/order_state.py` maps Tradier status strings to a typed `OrderState` enum and defines terminal states. `submit_and_verify` orchestrates place→poll→terminal/timeout with **injected `now`/`sleep`** for deterministic tests. Production wiring (a real `requests`-backed `http` + env-based token/base-url, C8) is a thin factory built last.

**Tech Stack:** Python 3.11, pytest, dataclasses/enum (stdlib). `requests` only in the production factory (not in tested logic).

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` §4 (stops must verify fills), §7 (broker adapter, order state machine, fill verification), §6 (env-only credentials, C8).

---

### Task 1: Broker package skeleton + core types

**Files:** Create `bot/broker/__init__.py`, `bot/broker/order_state.py`, `bot/tests/test_order_state.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_order_state.py`:
```python
from bot.broker.order_state import OrderState, TERMINAL, map_broker_status


def test_map_known_statuses():
    assert map_broker_status("filled") == OrderState.FILLED
    assert map_broker_status("open") == OrderState.OPEN
    assert map_broker_status("partially_filled") == OrderState.PARTIAL
    assert map_broker_status("canceled") == OrderState.CANCELLED
    assert map_broker_status("rejected") == OrderState.REJECTED
    assert map_broker_status("expired") == OrderState.EXPIRED


def test_unknown_status_maps_to_open():
    # unknown/pending -> treat as still-working (OPEN), never silently terminal
    assert map_broker_status("pending") == OrderState.OPEN
    assert map_broker_status("") == OrderState.OPEN


def test_terminal_set():
    assert OrderState.FILLED in TERMINAL
    assert OrderState.OPEN not in TERMINAL
    assert OrderState.TIMEOUT in TERMINAL
```

- [ ] **Step 2: Run, expect fail** — `python -m pytest bot/tests/test_order_state.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — `bot/broker/__init__.py`:
```python
"""Broker adapters for the S2b bot."""
```
`bot/broker/order_state.py`:
```python
"""Order lifecycle states and Tradier status mapping (spec §7)."""
from enum import Enum


class OrderState(str, Enum):
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIAL = "partially_filled"
    FILLED = "filled"
    CANCELLED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    TIMEOUT = "timeout"


TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
            OrderState.EXPIRED, OrderState.TIMEOUT}

_MAP = {
    "filled": OrderState.FILLED, "open": OrderState.OPEN,
    "partially_filled": OrderState.PARTIAL, "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED, "rejected": OrderState.REJECTED,
    "expired": OrderState.EXPIRED,
}


def map_broker_status(status: str) -> OrderState:
    """Map a Tradier order status to OrderState; unknown -> OPEN (never silently terminal)."""
    return _MAP.get((status or "").lower(), OrderState.OPEN)
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_order_state.py -v` → 3 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/broker/__init__.py bot/broker/order_state.py bot/tests/test_order_state.py
git commit -m "feat: order state enum + Tradier status mapping"
```

---

### Task 2: TradierClient.get_quote (injected transport)

**Files:** Create `bot/broker/tradier.py`, `bot/tests/test_tradier.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_tradier.py`:
```python
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
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.broker.tradier`.

- [ ] **Step 3: Implement** — `bot/broker/tradier.py`:
```python
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
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_tradier.py -v` → 1 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/broker/tradier.py bot/tests/test_tradier.py
git commit -m "feat: TradierClient.get_quote with injected transport"
```

---

### Task 3: place_order with broker-id verification (C6)

**Files:** Modify `bot/broker/tradier.py`, `bot/tests/test_tradier.py`

- [ ] **Step 1: Add failing tests** (append to `bot/tests/test_tradier.py`):
```python
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
```

- [ ] **Step 2: Run, expect fail** — `AttributeError: 'TradierClient' object has no attribute 'place_order'`.

- [ ] **Step 3: Implement** (append method to `TradierClient`):
```python
    def place_order(self, payload: dict) -> str:
        """Submit an order; return broker order id as str. Raise if no id (C6: no silent fail)."""
        resp = self.http("POST", f"/accounts/{self.account_id}/orders", data=payload)
        oid = resp.get("order", {}).get("id")
        if oid is None:
            raise BrokerError(f"order submission returned no id: {resp}")
        return str(oid)
```

- [ ] **Step 4: Run, expect pass** — 3 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/broker/tradier.py bot/tests/test_tradier.py
git commit -m "feat: place_order with broker-id verification (C6)"
```

---

### Task 4: get_order + cancel_order

**Files:** Modify `bot/broker/tradier.py`, `bot/tests/test_tradier.py`

- [ ] **Step 1: Add failing tests** (append):
```python
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
```

- [ ] **Step 2: Run, expect fail** — `AttributeError` on `get_order`.

- [ ] **Step 3: Implement** (append methods):
```python
    def get_order(self, order_id: str) -> dict:
        resp = self.http("GET", f"/accounts/{self.account_id}/orders/{order_id}")
        order = resp.get("order")
        if not order:
            raise BrokerError(f"no order {order_id}")
        return order

    def cancel_order(self, order_id: str) -> None:
        self.http("DELETE", f"/accounts/{self.account_id}/orders/{order_id}")
```

- [ ] **Step 4: Run, expect pass** — 5 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/broker/tradier.py bot/tests/test_tradier.py
git commit -m "feat: get_order and cancel_order"
```

---

### Task 5: submit_and_verify (place → poll → terminal / timeout-cancel)

**Files:** Create `bot/broker/submit.py`, `bot/tests/test_submit.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_submit.py`:
```python
from bot.broker.submit import submit_and_verify
from bot.broker.order_state import OrderState


class FakeClient:
    def __init__(self, statuses):
        self._statuses = list(statuses)   # status returned on each get_order call
        self.placed = None
        self.cancelled = None

    def place_order(self, payload):
        self.placed = payload
        return "999"

    def get_order(self, oid):
        return {"id": oid, "status": self._statuses.pop(0)}

    def cancel_order(self, oid):
        self.cancelled = oid


def _clock():
    t = {"now": 0.0}
    def now(): return t["now"]
    def sleep(s): t["now"] += s
    return now, sleep


def test_fills_on_second_poll():
    now, sleep = _clock()
    c = FakeClient(["open", "filled"])
    state, order = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=10.0, now=now, sleep=sleep)
    assert state == OrderState.FILLED
    assert c.placed == {"x": 1}
    assert c.cancelled is None


def test_times_out_and_cancels():
    now, sleep = _clock()
    c = FakeClient(["open", "open", "open", "open", "open", "open", "open", "open", "open", "open", "open", "open"])
    state, order = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=3.0, now=now, sleep=sleep)
    assert state == OrderState.TIMEOUT
    assert c.cancelled == "999"   # timed-out order is cancelled (no dangling order)


def test_rejected_is_terminal_no_cancel():
    now, sleep = _clock()
    c = FakeClient(["rejected"])
    state, _ = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=10.0, now=now, sleep=sleep)
    assert state == OrderState.REJECTED
    assert c.cancelled is None
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.broker.submit`.

- [ ] **Step 3: Implement** — `bot/broker/submit.py`:
```python
"""Submit an order and verify its terminal state, cancelling on timeout (spec §4, §7)."""
from bot.broker.order_state import map_broker_status, TERMINAL, OrderState


def submit_and_verify(client, payload, poll_s, timeout_s, now, sleep):
    """Place order, poll until terminal or timeout. On timeout, cancel. Returns (OrderState, order)."""
    oid = client.place_order(payload)
    start = now()
    while True:
        order = client.get_order(oid)
        state = map_broker_status(order.get("status"))
        if state in TERMINAL:
            return state, order
        if now() - start >= timeout_s:
            client.cancel_order(oid)
            return OrderState.TIMEOUT, order
        sleep(poll_s)
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_submit.py -v` → 3 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/broker/submit.py bot/tests/test_submit.py
git commit -m "feat: submit_and_verify with poll + timeout-cancel"
```

---

### Task 6: Production HTTP factory (env-based, C8) — thin, untested wiring

**Files:** Modify `bot/broker/tradier.py`, `bot/tests/test_tradier.py`

- [ ] **Step 1: Add failing test** (append to `bot/tests/test_tradier.py`) — verifies the factory reads env and refuses to run live without explicit opt-in:
```python
import os
from bot.broker.tradier import make_http_from_env


def test_make_http_requires_token(monkeypatch):
    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    with pytest.raises(BrokerError):
        make_http_from_env()


def test_make_http_returns_callable(monkeypatch):
    monkeypatch.setenv("TRADIER_TOKEN", "x")
    monkeypatch.setenv("TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
    http = make_http_from_env()
    assert callable(http)
```

- [ ] **Step 2: Run, expect fail** — `ImportError: make_http_from_env`.

- [ ] **Step 3: Implement** (append to `bot/broker/tradier.py`):
```python
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
```

- [ ] **Step 4: Run, expect pass** — note: `test_make_http_returns_callable` requires `requests` installed; if absent, `pip install requests` first. Expected: 7 passed in this file.

- [ ] **Step 5: Commit**
```bash
git add bot/broker/tradier.py bot/tests/test_tradier.py
git commit -m "feat: env-based Tradier http factory (C8)"
```

---

### Task 7: Full-suite green + integration smoke

**Files:** `bot/tests/test_submit.py`

- [ ] **Step 1: Add a wiring test** (append to `bot/tests/test_submit.py`) — `TradierClient` + fake http drives `submit_and_verify` end to end:
```python
from bot.broker.tradier import TradierClient


def test_tradier_client_drives_submit_and_verify():
    seq = iter([{"order": {"id": 999, "status": "ok"}},       # place
                {"order": {"id": 999, "status": "open"}},      # poll 1
                {"order": {"id": 999, "status": "filled"}}])   # poll 2

    def http(method, path, params=None, data=None):
        return next(seq)

    now, sleep = _clock()
    c = TradierClient(account_id="ABC", http=http)
    state, order = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=10.0, now=now, sleep=sleep)
    assert state == OrderState.FILLED
```

- [ ] **Step 2: Run the FULL suite** — `python -m pytest` → expected 27 (Plan 1) + 11 (Plan 2) = 38 passed.

- [ ] **Step 3: Commit**
```bash
git add bot/tests/test_submit.py
git commit -m "test: TradierClient drives submit_and_verify end-to-end"
```

---

## Self-review notes (done)
- **Spec coverage:** §7 broker adapter (quote/place/get/cancel) → Tasks 2–4; C6 broker-id verification → Task 3; §4/§7 fill verification + timeout-cancel order state machine → Tasks 1, 5; §6/C8 env-only credentials → Task 6.
- **No placeholders:** every step has complete code, commands, expected counts.
- **Type consistency:** `http(method, path, params, data)` signature identical across `TradierClient` methods, the fake in tests, and `make_http_from_env`; `OrderState`/`TERMINAL`/`map_broker_status` defined in Task 1 and reused in Task 5.
- **Hermetic:** all logic tests inject `http` and `now`/`sleep`; `requests` is imported lazily only inside `make_http_from_env`, so the tested code never needs network or the dep.

## Next: Plan 3 — S2b strategy + cushion gate (produces the `SpreadOrder`/payload fed to this adapter and the Plan-1 gate).
