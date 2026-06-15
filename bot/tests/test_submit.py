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
