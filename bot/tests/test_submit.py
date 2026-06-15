from bot.broker.submit import submit_and_verify
from bot.broker.order_state import OrderState
from bot.broker.tradier import BrokerError


class FakeClient:
    def __init__(self, statuses, cancel_raises=False):
        self._statuses = list(statuses)   # status returned on each get_order call
        self.placed = None
        self.cancelled = None
        self._cancel_raises = cancel_raises

    def place_order(self, payload):
        self.placed = payload
        return "999"

    def get_order(self, oid):
        return {"id": oid, "status": self._statuses.pop(0)}

    def cancel_order(self, oid):
        self.cancelled = oid
        if self._cancel_raises:
            raise BrokerError("order already in filled state")


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
    # After cancel, submit_and_verify re-queries — need one extra "open" for that re-query
    c = FakeClient(["open", "open", "open", "open", "open", "open", "open", "open", "open", "open", "open", "open", "open"])
    state, order = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=3.0, now=now, sleep=sleep)
    assert state == OrderState.TIMEOUT
    assert c.cancelled == "999"   # timed-out order is cancelled (no dangling order)


def test_rejected_is_terminal_no_cancel():
    now, sleep = _clock()
    c = FakeClient(["rejected"])
    state, _ = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=10.0, now=now, sleep=sleep)
    assert state == OrderState.REJECTED
    assert c.cancelled is None


# Fix B: submit_and_verify must re-query after timeout-cancel and honor a fill that won the race
def test_timeout_but_fill_wins_race():
    """When cancel raises BrokerError (order just filled), re-query must return FILLED."""
    now, sleep = _clock()
    # poll_s=1.0, timeout_s=0.5 → timeout fires immediately on first get_order check
    # statuses: two "open" polls hit before timeout, then re-query after cancel returns "filled"
    c = FakeClient(["open", "open", "filled"], cancel_raises=True)
    state, order = submit_and_verify(c, {"x": 1}, poll_s=1.0, timeout_s=0.5, now=now, sleep=sleep)
    assert state == OrderState.FILLED
    assert c.cancelled == "999"   # cancel was attempted even though it raised


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
