"""Submit an order and verify its terminal state, cancelling on timeout (spec §4, §7)."""
import dataclasses

from bot.broker.order_state import map_broker_status, TERMINAL, OrderState, ExecutionResult
from bot.broker.tradier import BrokerError


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
            try:
                client.cancel_order(oid)
            except BrokerError:
                pass  # cancel can fail if the order just filled — re-query below
            final = client.get_order(oid)
            fstate = map_broker_status(final.get("status"))
            if fstate in TERMINAL:
                return fstate, final   # order reached a real terminal state (e.g. FILLED won the race)
            return OrderState.TIMEOUT, final
        sleep(poll_s)


def _no_order_result():
    """Result for the case where the ladder aborts before ever placing an order."""
    return ExecutionResult(status="aborted", requested_quantity=0, filled_quantity=0,
                           average_fill_price=None, submitted_limit=None, order_id=None)


def submit_entry_ladder(place_fn, cancel_fn, start_credit, min_credit, step, max_seconds,
                        now_fn, sleep_fn, on_reprice=None, should_abort=None):
    """Ladder from start_credit down toward min_credit in `step` decrements (partner spec §6).

    place_fn(credit) -> ExecutionResult (status 'filled'/'open'/'partially_filled'/'rejected', ...).
    In production `place_fn` submits ONE multileg limit order and blocks (polling) for the dwell
    window (features.entry_reprice_seconds) before returning whatever status it currently has --
    submit_entry_ladder itself never sleeps between rungs; it only paces the abort/max_seconds
    checks against now_fn(), so timing is entirely owned by the injected place_fn/sleep_fn.

    cancel_fn(order_id) -> True once cancellation is CONFIRMED (must confirm before re-placing).
    A confirmed cancel happens unconditionally for any still-open rung before the ladder ever
    considers a lower credit or gives up -- so this function NEVER holds two live opening orders.

    on_reprice(new_credit) -> optionally refreshes quotes/qty; may return a dict
    {'credit':.., 'qty':.., 'abort':bool} to override the next credit or force an abort.

    should_abort() -> True to abort immediately (stale/wide/spot-move/ratio/cost/budget/chain
    change). Checked before the very first order and again after every cancel-confirm, so an
    abort never leaves an order behind.

    Returns the final ExecutionResult (filled, partial, or unfilled/cancelled). Stops after
    max_seconds (via now_fn), on fill, on abort, or once the next rung would cross below
    min_credit. On partial fill, stops and returns the partial result -- the remainder is never
    silently re-submitted."""
    start_time = now_fn()
    credit = round(start_credit, 2)

    if should_abort is not None and should_abort():
        return _no_order_result()

    while True:
        result = place_fn(credit)

        if result.status == "filled":
            return result
        if result.status == "rejected":
            return result
        if result.status == "partially_filled" or result.status == "partial":
            if result.order_id is not None:
                cancel_fn(result.order_id)
            return result

        # Still working (status "open" or anything else non-terminal): cancel + CONFIRM before
        # doing anything else -- never leave this rung's order live while we decide what's next.
        confirmed = True
        if result.order_id is not None:
            confirmed = cancel_fn(result.order_id)

        if should_abort is not None and should_abort():
            return dataclasses.replace(result, status="canceled")
        if not confirmed:
            # Can't safely reprice without a confirmed cancel (e.g. the broker reports the order
            # actually filled during the race) -- stop here rather than risk two live orders.
            return dataclasses.replace(result, status="canceled")
        if now_fn() - start_time >= max_seconds:
            return dataclasses.replace(result, status="canceled")

        next_credit = round(credit - step, 2)
        if on_reprice is not None:
            info = on_reprice(next_credit) or {}
            if info.get("abort"):
                return dataclasses.replace(result, status="canceled")
            if "credit" in info:
                next_credit = round(info["credit"], 2)

        if next_credit < round(min_credit, 2) - 1e-9:
            return dataclasses.replace(result, status="canceled")   # never cross below min_credit

        credit = next_credit
