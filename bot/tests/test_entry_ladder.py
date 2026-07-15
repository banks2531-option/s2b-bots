"""TDD for Task 3.1: midpoint->natural entry limit ladder (partner spec §6).

The core (bot.broker.submit.submit_entry_ladder) is pure/injectable: no real network, no real
time. `place_fn` stands in for "submit one multileg limit order, then block (poll) for the dwell
window and return whatever status results" -- so a fake place_fn advances the injected clock
itself (via sleep_fn) to simulate that dwell, exactly like the production wiring will."""
import dataclasses

from bot.broker.submit import submit_entry_ladder
from bot.broker.order_state import ExecutionResult


def _clock():
    t = {"now": 0.0}
    def now(): return t["now"]
    def sleep(s): t["now"] += s
    return now, sleep


def _res(status, order_id, filled_qty=0, requested_qty=10, price=None):
    return ExecutionResult(status=status, requested_quantity=requested_qty,
                           filled_quantity=filled_qty, average_fill_price=price,
                           submitted_limit=price, order_id=order_id)


class FakeLadderBroker:
    """Scripts one ExecutionResult per place_fn() call (order_id auto-assigned) and records every
    place/cancel event in order, so tests can assert never-two-live-orders sequencing."""

    def __init__(self, scripted_statuses, now_fn, sleep_fn, dwell=5.0, cancel_confirms=True,
                filled_qty_on_fill=10, requested_qty=10):
        self.scripted = list(scripted_statuses)
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.dwell = dwell
        self.cancel_confirms = cancel_confirms
        self.filled_qty_on_fill = filled_qty_on_fill
        self.requested_qty = requested_qty
        self.events = []          # [("place", credit, order_id), ("cancel", order_id)]
        self.credits_placed = []
        self._next_id = 100
        self.cancel_calls = 0

    def place_fn(self, credit):
        self.sleep_fn(self.dwell)   # simulate production place_fn's internal submit+poll dwell
        oid = str(self._next_id); self._next_id += 1
        status = self.scripted.pop(0)
        self.events.append(("place", credit, oid))
        self.credits_placed.append(credit)
        filled_qty = self.filled_qty_on_fill if status == "filled" else 0
        if status == "partially_filled":
            filled_qty = self.filled_qty_on_fill // 2
        return _res(status, oid, filled_qty=filled_qty, requested_qty=self.requested_qty,
                   price=credit)

    def cancel_fn(self, order_id):
        self.cancel_calls += 1
        self.events.append(("cancel", order_id))
        return self.cancel_confirms


# ── (a) fills at the first rung ──────────────────────────────────────────────────────────────────

def test_fills_at_first_rung():
    now, sleep = _clock()
    broker = FakeLadderBroker(["filled"], now, sleep)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.70, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status == "filled"
    assert result.filled_quantity == 10
    assert broker.credits_placed == [1.80]
    assert broker.cancel_calls == 0   # a fill needs no cancel


# ── (b) steps down $0.01 every interval and fills at a lower rung ───────────────────────────────

def test_steps_down_a_penny_per_rung_and_fills_lower():
    now, sleep = _clock()
    broker = FakeLadderBroker(["open", "open", "filled"], now, sleep)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.70, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status == "filled"
    assert broker.credits_placed == [1.80, 1.79, 1.78]


# ── (c) never places a new order before the prior is cancel-confirmed ───────────────────────────

def test_never_places_new_order_before_prior_cancel_confirmed():
    now, sleep = _clock()
    broker = FakeLadderBroker(["open", "open", "filled"], now, sleep)
    submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80, min_credit=1.70,
                       step=0.01, max_seconds=30, now_fn=now, sleep_fn=sleep)
    # every "place" event except the first must be immediately preceded by a "cancel" of the
    # order id from the previous "place" event -- i.e. no two live orders back to back.
    placed_ids = [e for e in broker.events if e[0] == "place"]
    for i in range(1, len(placed_ids)):
        prev_oid = placed_ids[i - 1][2]
        idx_of_this_place = broker.events.index(placed_ids[i])
        preceding = broker.events[idx_of_this_place - 1]
        assert preceding == ("cancel", prev_oid), broker.events


# ── (d) never submits below min_credit — returns unfilled if min reached without a fill ─────────

def test_never_submits_below_min_credit_returns_unfilled():
    now, sleep = _clock()
    # start 1.80, min 1.78, step 0.01 -> rungs 1.80, 1.79, 1.78 all tried and never fill;
    # 1.77 would cross below min_credit so it must never be placed.
    broker = FakeLadderBroker(["open", "open", "open", "open", "open"], now, sleep)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.78, step=0.01, max_seconds=999,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status != "filled"
    assert broker.credits_placed == [1.80, 1.79, 1.78]
    assert min(broker.credits_placed) >= 1.78
    assert broker.cancel_calls == 3   # every rung, including the last, was cancel-confirmed


# ── (e) stops at max_seconds ─────────────────────────────────────────────────────────────────────

def test_stops_at_max_seconds():
    now, sleep = _clock()
    broker = FakeLadderBroker(["open"] * 10, now, sleep, dwell=5.0)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=0.0, step=0.01, max_seconds=12.0,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status != "filled"
    # dwell=5s/rung: rung1 -> t=5 (continue), rung2 -> t=10 (continue), rung3 -> t=15 (>=12, stop)
    assert len(broker.credits_placed) == 3
    assert now() >= 12.0


# ── (f) should_abort True aborts immediately and cancels any working order ──────────────────────

def test_should_abort_true_before_any_placement_aborts_with_no_order():
    now, sleep = _clock()
    broker = FakeLadderBroker(["filled"], now, sleep)   # would fill if ever placed -- must not run
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.70, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep, should_abort=lambda: True)
    assert result.status != "filled"
    assert broker.credits_placed == []
    assert broker.cancel_calls == 0


def test_should_abort_true_after_a_rung_cancels_the_working_order_and_stops():
    now, sleep = _clock()
    broker = FakeLadderBroker(["open", "filled"], now, sleep)   # 2nd rung would fill if reached
    calls = {"n": 0}
    def should_abort():
        calls["n"] += 1
        return calls["n"] >= 2   # 1st check is pre-loop (must pass); abort on the 2nd (post-cancel)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.70, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep, should_abort=should_abort)
    assert result.status != "filled"
    assert broker.credits_placed == [1.80]         # never reached the 2nd (would-fill) rung
    assert broker.cancel_calls == 1                # the first rung's working order was cancelled


# ── (g) partial fill stops and returns the partial ──────────────────────────────────────────────

def test_partial_fill_stops_and_returns_the_partial():
    now, sleep = _clock()
    broker = FakeLadderBroker(["partially_filled", "filled"], now, sleep, filled_qty_on_fill=10)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.70, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status == "partially_filled"
    assert result.filled_quantity == 5              # half of 10 -- never assumed == requested
    assert broker.credits_placed == [1.80]           # stopped, did not keep repricing the remainder
    assert broker.cancel_calls == 1                  # the partially-filled working order was cancelled


# ── on_reprice: abort flag stops the ladder like should_abort ───────────────────────────────────

def test_on_reprice_abort_flag_stops_the_ladder():
    now, sleep = _clock()
    broker = FakeLadderBroker(["open", "filled"], now, sleep)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.70, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep,
                                 on_reprice=lambda c: {"abort": True})
    assert result.status != "filled"
    assert broker.credits_placed == [1.80]


def test_on_reprice_can_override_the_next_credit():
    now, sleep = _clock()
    broker = FakeLadderBroker(["open", "filled"], now, sleep)
    result = submit_entry_ladder(broker.place_fn, broker.cancel_fn, start_credit=1.80,
                                 min_credit=1.50, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep,
                                 on_reprice=lambda c: {"credit": 1.65})
    assert result.status == "filled"
    assert broker.credits_placed == [1.80, 1.65]
