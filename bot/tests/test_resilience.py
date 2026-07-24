"""Resilience to Tradier API failures (2026-07-24 Bot C crash-loop + sticky-halt root cause).

Two guarantees:
1. Idempotent GETs retry transient failures then surface MarketDataUnavailable; ORDERS are never retried.
2. A market-data gap SKIPS a tick (no crash) and does NOT set the sticky "failed close" halt; a genuine
   failed close ORDER still halts.
"""
from datetime import datetime

import pytest

from bot.errors import MarketDataUnavailable
from bot.broker.tradier import build_http, _get_with_retry
from bot.app.orchestrator import BotState, Deps, run_management_cycle, run_entry_cycle, tick
from bot.strategy.manage import ManagedPosition, ExitAction
from bot.strategy.s2b import OptionQuote, S2bConfig
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


# ---------------- http retry / backoff (idempotent GET only) ----------------

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.reason = "reason"
        self._payload = payload if payload is not None else {"ok": True}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_get_retries_on_5xx_then_succeeds():
    seq = [_Resp(503), _Resp(500), _Resp(200, {"quotes": 1})]
    calls = []
    def req(method, url, params, data):
        calls.append(method)
        return seq[len(calls) - 1]
    http = build_http(req, "https://x", sleep_fn=lambda s: None)
    assert http("GET", "/markets/quotes") == {"quotes": 1}
    assert calls == ["GET", "GET", "GET"]              # retried past the two 5xx


def test_get_raises_market_data_unavailable_after_exhausting_retries():
    http = build_http(lambda m, u, p, d: _Resp(503), "https://x", sleep_fn=lambda s: None)
    with pytest.raises(MarketDataUnavailable):
        http("GET", "/markets/history")


def test_orders_are_issued_once_and_never_retried():
    # A POST/DELETE that fails must NOT be retried -- a timed-out order may have executed at the broker.
    calls = []
    def req(method, url, params, data):
        calls.append(method)
        raise RuntimeError("gateway blip")
    http = build_http(req, "https://x", sleep_fn=lambda s: None)
    with pytest.raises(RuntimeError):
        http("POST", "/accounts/x/orders", data={})
    assert calls == ["POST"]                            # exactly one attempt


def test_get_with_retry_retries_transient_exception_then_succeeds():
    class FakeTimeout(Exception):
        pass
    seq = [FakeTimeout(), _Resp(200, {"v": 2})]
    calls = []
    def req():
        calls.append(1)
        r = seq[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    out = _get_with_retry(req, sleep_fn=lambda s: None, transient=(FakeTimeout,))
    assert out == {"v": 2} and len(calls) == 2


# ---------------- tick / management resilience ----------------

def _chain():
    return [OptionQuote(572.0, 0.45, 4.50, 4.60), OptionQuote(568.0, 0.36, 3.40, 3.50),
            OptionQuote(565.0, 0.30, 2.80, 2.90), OptionQuote(560.0, 0.22, 2.00, 2.10),
            OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
        get_chain=lambda s, e: _chain(), pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None, s2b_cfg=S2bConfig(),
    )
    base.update(over)
    return Deps(**base)


def test_tick_skips_gracefully_when_market_data_unavailable():
    # get_atr raises (empty bars) -> the whole tick must SKIP, not crash, and must not halt.
    def bad_atr(s):
        raise MarketDataUnavailable("no bars")
    alerts = []
    d = _deps(get_atr=bad_atr, alert_sink=lambda a: alerts.extend(a))
    out = tick(BotState(), d, datetime(2026, 6, 15, 10, 5))   # must NOT raise
    assert isinstance(out, BotState) and out.halted is False
    assert any("market data unavailable" in a.message for a in alerts)


def test_mark_failure_does_not_set_the_sticky_halt():
    # A quote gap while marking a held position -> ERROR(mark_unavailable) -> WARN + retry, NOT a halt.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=0.74, qty=1, expiry="2026-06-19")
    def bad_mark(p):
        raise MarketDataUnavailable("no market for SPY...P00558000")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=bad_mark)
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.ERROR and results[0].mark_unavailable is True
    assert state.halted is False                        # the fix: a data gap must not strand the bot


def test_fetch_atr_empty_history_raises_typed_error():
    from bot.app.run_s2b import fetch_atr
    http = lambda *a, **k: {"history": {"day": []}}     # Tradier 200 with no bars
    with pytest.raises(MarketDataUnavailable):
        fetch_atr(http, "SPY", "2026-06-15")
