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
