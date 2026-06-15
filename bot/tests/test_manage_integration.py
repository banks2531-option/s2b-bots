"""The bot-B lesson, pinned: when a stop triggers, the close is executed AND verified filled."""
from bot.strategy.manage import (ManagedPosition, ManageConfig, monitor_positions,
                                 build_close_payload, spread_value_mid, ExitAction)
from bot.strategy.s2b import OptionQuote
from bot.broker.submit import submit_and_verify
from bot.broker.order_state import OrderState


def _clock():
    t = {"now": 0.0}
    return (lambda: t["now"]), (lambda s: t.__setitem__("now", t["now"] + s))


class _FakeBroker:
    """Records the submitted close order; reports place -> filled across two get_order calls."""
    def __init__(self, fills):
        self._fills = fills
        self._seq = iter([{"id": 777, "status": "ok"},        # place ack
                          {"id": 777, "status": "filled"}])    # poll -> filled
    def place_order(self, payload):
        self._fills.append(payload)
        return "777"
    def get_order(self, oid):
        return next(self._seq)
    def cancel_order(self, oid):
        pass


def test_stop_triggers_close_and_verifies_fill():
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    cfg = ManageConfig()
    # mark the spread from quotes: short blew out -> spread mid 9.00 (== the 9.0 stop level)
    short_q = OptionQuote(568.0, 0.80, 10.40, 10.60)   # mid 10.50
    long_q = OptionQuote(558.0, 0.40, 1.40, 1.60)      # mid 1.50

    fills = []

    def mark_fn(p):
        return spread_value_mid(short_q, long_q)        # 9.0 -> STOP

    def dte_fn(p):
        return 5

    def close_fn(p, action):
        now, sleep = _clock()
        payload = build_close_payload(p, limit_price=9.10)
        state, _ = submit_and_verify(_FakeBroker(fills), payload,
                                     poll_s=1.0, timeout_s=10.0, now=now, sleep=sleep)
        return state.value                               # "filled"

    results = monitor_positions([pos], mark_fn, dte_fn, close_fn, cfg)
    assert len(results) == 1
    assert results[0].action == ExitAction.STOP
    assert results[0].close_status == OrderState.FILLED.value
    assert results[0].failed is False
    # the close order was actually built and submitted as a debit buy-to-close
    assert fills and fills[0]["type"] == "debit" and fills[0]["side[0]"] == "buy_to_close"
