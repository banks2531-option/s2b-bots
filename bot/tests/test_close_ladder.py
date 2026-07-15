"""TDD for Task 3.2: take-profit close ladder, mid->natural DEBIT ladder (partner spec §7).

Mirrors test_entry_ladder.py's fake-broker harness, but a close is a DEBIT: the ladder starts at
start_debit (package mid, the cheapest/best price for the closer) and steps UP toward max_debit
(natural, the most marketable/worst price) -- the opposite direction of the entry credit ladder."""
import dataclasses

from bot.broker.submit import submit_close_ladder
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


class FakeCloseLadderBroker:
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
        self.events = []          # [("place", debit, order_id), ("cancel", order_id)]
        self.debits_placed = []
        self._next_id = 100
        self.cancel_calls = 0

    def place_fn(self, debit):
        self.sleep_fn(self.dwell)   # simulate production place_fn's internal submit+poll dwell
        oid = str(self._next_id); self._next_id += 1
        status = self.scripted.pop(0)
        self.events.append(("place", debit, oid))
        self.debits_placed.append(debit)
        filled_qty = self.filled_qty_on_fill if status == "filled" else 0
        if status == "partially_filled":
            filled_qty = self.filled_qty_on_fill // 2
        return _res(status, oid, filled_qty=filled_qty, requested_qty=self.requested_qty,
                   price=debit)

    def cancel_fn(self, order_id):
        self.cancel_calls += 1
        self.events.append(("cancel", order_id))
        return self.cancel_confirms


# ── (a) fills at the first rung (the mid) ────────────────────────────────────────────────────────

def test_fills_at_mid():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["filled"], now, sleep)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.80, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status == "filled"
    assert result.filled_quantity == 10
    assert broker.debits_placed == [1.70]
    assert broker.cancel_calls == 0   # a fill needs no cancel


# ── (b) steps UP a penny every interval and fills at a higher (more marketable) rung ─────────────

def test_steps_up_a_penny_per_rung_toward_natural_and_fills():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["open", "open", "filled"], now, sleep)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.80, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status == "filled"
    assert broker.debits_placed == [1.70, 1.71, 1.72]


# ── (c) never places a new order before the prior is cancel-confirmed ───────────────────────────

def test_never_places_new_order_before_prior_cancel_confirmed():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["open", "open", "filled"], now, sleep)
    submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70, max_debit=1.80,
                        step=0.01, max_seconds=30, now_fn=now, sleep_fn=sleep)
    placed_ids = [e for e in broker.events if e[0] == "place"]
    for i in range(1, len(placed_ids)):
        prev_oid = placed_ids[i - 1][2]
        idx_of_this_place = broker.events.index(placed_ids[i])
        preceding = broker.events[idx_of_this_place - 1]
        assert preceding == ("cancel", prev_oid), broker.events


# ── (d) never exceeds max_debit (natural) — returns unfilled if max reached without a fill ──────

def test_never_exceeds_max_debit_returns_unfilled():
    now, sleep = _clock()
    # start 1.70, max 1.72, step 0.01 -> rungs 1.70, 1.71, 1.72 all tried and never fill;
    # 1.73 would cross above max_debit so it must never be placed.
    broker = FakeCloseLadderBroker(["open", "open", "open", "open", "open"], now, sleep)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.72, step=0.01, max_seconds=999,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status != "filled"
    assert broker.debits_placed == [1.70, 1.71, 1.72]
    assert max(broker.debits_placed) <= 1.72
    assert broker.cancel_calls == 3   # every rung, including the last, was cancel-confirmed


# ── (e) stops at max_seconds ─────────────────────────────────────────────────────────────────────

def test_stops_at_max_seconds():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["open"] * 10, now, sleep, dwell=5.0)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=2.50, step=0.01, max_seconds=12.0,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status != "filled"
    # dwell=5s/rung: rung1 -> t=5 (continue), rung2 -> t=10 (continue), rung3 -> t=15 (>=12, stop)
    assert len(broker.debits_placed) == 3
    assert now() >= 12.0


# ── (f) should_abort True aborts immediately and cancels any working order ──────────────────────

def test_should_abort_true_before_any_placement_aborts_with_no_order():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["filled"], now, sleep)   # would fill if ever placed -- must not run
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.80, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep, should_abort=lambda: True)
    assert result.status != "filled"
    assert broker.debits_placed == []
    assert broker.cancel_calls == 0


def test_should_abort_true_after_a_rung_cancels_the_working_order_and_stops():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["open", "filled"], now, sleep)   # 2nd rung would fill if reached
    calls = {"n": 0}
    def should_abort():
        calls["n"] += 1
        return calls["n"] >= 2   # 1st check is pre-loop (must pass); abort on the 2nd (post-cancel)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.80, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep, should_abort=should_abort)
    assert result.status != "filled"
    assert broker.debits_placed == [1.70]         # never reached the 2nd (would-fill) rung
    assert broker.cancel_calls == 1                # the first rung's working order was cancelled


# ── (g) partial fill stops and returns the partial ──────────────────────────────────────────────

def test_partial_fill_stops_and_returns_the_partial():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["partially_filled", "filled"], now, sleep, filled_qty_on_fill=10)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.80, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep)
    assert result.status == "partially_filled"
    assert result.filled_quantity == 5              # half of 10 -- never assumed == requested
    assert broker.debits_placed == [1.70]            # stopped, did not keep repricing the remainder
    assert broker.cancel_calls == 1                  # the partially-filled working order was cancelled


# ── on_reprice: abort flag stops the ladder like should_abort ───────────────────────────────────

def test_on_reprice_abort_flag_stops_the_ladder():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["open", "filled"], now, sleep)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=1.80, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep,
                                 on_reprice=lambda d: {"abort": True})
    assert result.status != "filled"
    assert broker.debits_placed == [1.70]


def test_on_reprice_can_override_the_next_debit():
    now, sleep = _clock()
    broker = FakeCloseLadderBroker(["open", "filled"], now, sleep)
    result = submit_close_ladder(broker.place_fn, broker.cancel_fn, start_debit=1.70,
                                 max_debit=2.00, step=0.01, max_seconds=30,
                                 now_fn=now, sleep_fn=sleep,
                                 on_reprice=lambda d: {"debit": 1.90})
    assert result.status == "filled"
    assert broker.debits_placed == [1.70, 1.90]
